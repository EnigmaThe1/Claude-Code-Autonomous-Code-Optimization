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

import copy
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from accepted_task import persist_accepted_task_record
from authority_set import build_authority_snapshot
from cli_schema import build_parser
from governance_contract import canonical_json_bytes
from repo_identity import repo_state_dir
from repo_runtime import activate
from shadow_validation import (
    MAX_LEGACY_OBSERVATION_BYTES,
    ShadowValidationError,
    compare_shadow_observations,
    compute_rc4_shadow_observation,
    load_legacy_shadow_observation,
    shadow_compare,
)
from state_adoption import import_legacy_state_claims
from state_store import json_dump, load_json
from task_sources import resolve_task_sources
from task_workspace import begin_task_workspace, load_active_task_workspace


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
    _run(root, "git", "config", "user.name", "Shadow Test")
    _run(root, "git", "config", "user.email", "shadow@example.invalid")
    (root / "PLAN.md").write_text("# plan\n")
    _run(root, "git", "add", "PLAN.md")
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
        "verification": ["verify task"],
        "commit_subject": None,
        "metadata": {},
    }


def _configure_json_tasks(root: Path) -> None:
    tasks = root / "tasks.json"
    tasks.write_text(json.dumps({
        "schema_version": 1,
        "tasks": [
            _task("T1"),
            _task("T2", depends_on=["T1"]),
        ],
    }) + "\n")
    governance = {
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
                "id": "json-ledger",
                "kind": "json",
                "authority_sets": ["default"],
                "paths": ["tasks.json"],
            }],
            "execution_mode": "single-writer",
            "strict_dependencies": True,
        },
        "control_surfaces": [],
    }
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(governance, indent=2) + "\n")
    _run(root, "git", "add", "tasks.json", ".claude-auto/governance.json")
    _run(root, "git", "commit", "-qm", "governed task ledger")


def _git_truth(root: Path) -> tuple[str, str]:
    status = _run(
        root,
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    refs = _run(
        root,
        "git",
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
    ).stdout
    return status, refs


def _state_files(state_root: Path) -> list[str]:
    if not state_root.exists():
        return []
    return sorted(
        str(path.relative_to(state_root))
        for path in state_root.rglob("*")
        if path.is_file()
    )


def test_shadow_snapshot_is_read_only_and_derives_json_task_frontier(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        _configure_json_tasks(root)
        state_root = repo_state_dir(root)

        git_before = _git_truth(root)
        files_before = _state_files(state_root)
        observation = compute_rc4_shadow_observation(root)
        git_after = _git_truth(root)
        files_after = _state_files(state_root)

        assert git_after == git_before
        assert files_after == files_before
        assert observation["product_sha"] == _run(
            root, "git", "rev-parse", "HEAD"
        ).stdout.strip()
        assert observation["authority"]["status"] == "READY"
        assert observation["authority"]["set_ids"] == ["default"]
        assert observation["task_source"]["status"] == "READY"
        assert [
            row["id"] for row in observation["task_source"]["task_graph"]
        ] == ["T1", "T2"]
        assert observation["ready_frontier"] == ["T1"]
        assert observation["active_task"] is None
        assert observation["next_task"]["id"] == "T1"
        assert observation["accepted_tasks"]["validated_ids"] == []
        assert observation["planning_repair"] is None
        assert len(observation["observation_sha256"]) == 64
        assert not (state_root / "git-trust").exists()
        assert not (state_root / "tasks" / "task-source-set.json").exists()


def test_shadow_compare_match_mismatch_reviewed_and_incomplete(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        _configure_json_tasks(root)
        observation = compute_rc4_shadow_observation(root)
        git_before = _git_truth(root)

        legacy_path = Path(td) / "legacy.json"
        legacy_path.write_text(json.dumps(observation))
        matched = shadow_compare(root, legacy_path)
        assert matched["status"] == "MATCH"
        assert matched["comparison"]["mismatch_count"] == 0
        assert matched["comparison"]["evidence_gap_count"] == 0
        audit_path = Path(matched["audit"]["audit_path"])
        assert audit_path.is_file()
        assert audit_path.is_relative_to(repo_state_dir(root) / "shadow" / "audits")
        assert _git_truth(root) == git_before

        mismatched_obs = copy.deepcopy(observation)
        mismatched_obs["ready_frontier"] = ["T2"]
        legacy_path.write_text(json.dumps(mismatched_obs))
        mismatch = shadow_compare(root, legacy_path)
        assert mismatch["status"] == "MISMATCH"
        rows = {
            row["dimension"]: row
            for row in mismatch["comparison"]["results"]
        }
        assert rows["ready_frontier"]["classification"] == "MISMATCH"
        assert "ready_frontier" in mismatch["comparison"][
            "unresolved_mismatches"
        ]

        dispositions = Path(td) / "dispositions.json"
        dispositions.write_text(json.dumps({
            "ready_frontier": "Legacy queue intentionally reports a stale scheduling frontier.",
        }))
        reviewed = shadow_compare(
            root,
            legacy_path,
            dispositions_path=dispositions,
        )
        assert reviewed["status"] == "REVIEWED"
        assert reviewed["comparison"]["unresolved_mismatch_count"] == 0
        reviewed_rows = {
            row["dimension"]: row
            for row in reviewed["comparison"]["results"]
        }
        assert reviewed_rows["ready_frontier"]["classification"] == "MISMATCH"
        assert reviewed_rows["ready_frontier"]["disposition"]

        incomplete_obs = copy.deepcopy(observation)
        del incomplete_obs["blockers"]
        comparison = compare_shadow_observations(
            observation,
            incomplete_obs,
        )
        assert comparison["evidence_gap_count"] == 1
        missing = {
            row["dimension"]: row
            for row in comparison["results"]
        }
        assert missing["blockers"]["classification"] == (
            "MISSING_LEGACY_EVIDENCE"
        )
        legacy_path.write_text(json.dumps(incomplete_obs))
        incomplete = shadow_compare(root, legacy_path)
        assert incomplete["status"] == "INCOMPLETE"
        assert _git_truth(root) == git_before


def test_shadow_legacy_input_is_bounded_regular_data_only():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        valid = base / "legacy.json"
        valid.write_text('{"schema_version":1,"product_sha":"abc"}')
        assert load_legacy_shadow_observation(valid)["product_sha"] == "abc"

        too_large = base / "large.json"
        too_large.write_bytes(b"{" + b"x" * MAX_LEGACY_OBSERVATION_BYTES + b"}")
        with pytest.raises(ShadowValidationError, match="2 MiB"):
            load_legacy_shadow_observation(too_large)

        if hasattr(os, "symlink"):
            link = base / "legacy-link.json"
            link.symlink_to(valid)
            with pytest.raises(ShadowValidationError, match="non-symlink"):
                load_legacy_shadow_observation(link)


def test_shadow_cli_parser_exposes_snapshot_and_compare():
    parser = build_parser("test")
    snapshot = parser.parse_args(["shadow", "snapshot"])
    assert snapshot.command == "shadow"
    assert snapshot.shadow_command == "snapshot"

    compare = parser.parse_args([
        "shadow",
        "compare",
        "--legacy",
        "legacy-observation.json",
        "--dispositions",
        "review.json",
    ])
    assert compare.shadow_command == "compare"
    assert compare.legacy == "legacy-observation.json"
    assert compare.dispositions == "review.json"


def test_p7_complex_multiledger_shadow_matches_independent_legacy_observation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")

        for rel, content in (
            ("plans/domain-a.md", "# domain a\n"),
            ("plans/domain-b.md", "# domain b\n"),
            ("generated/projection.json", '{"projection":"stable"}\n'),
        ):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        def task(
            task_id: str,
            authority_sets: list[str],
            owned_paths: list[str],
            depends_on: list[str] | None = None,
        ) -> dict:
            return {
                "schema_version": 1,
                "id": task_id,
                "authority_sets": authority_sets,
                "depends_on": depends_on or [],
                "owned_paths": owned_paths,
                "evidence_paths": [f"evidence/{task_id}/**"],
                "runtime_scratch_paths": [f".scratch/{task_id}/**"],
                "verification": [f"verify {task_id}"],
                "commit_subject": None,
                "metadata": {},
            }

        ledger_a = {
            "schema_version": 1,
            "tasks": [
                task("T0", ["domain-a"], ["packages/a/base/**"]),
                task(
                    "T2",
                    ["domain-a", "domain-b"],
                    ["packages/a/cross/**", "packages/b/cross/**"],
                    depends_on=["T1"],
                ),
            ],
        }
        ledger_b = {
            "schema_version": 1,
            "tasks": [
                task(
                    "T1",
                    ["domain-b"],
                    ["packages/b/service/**"],
                    depends_on=["T0"],
                ),
            ],
        }
        for rel, value in (
            ("tasks/domain-a.json", ledger_a),
            ("tasks/domain-b.json", ledger_b),
        ):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2) + "\n")

        governance = {
            "schema_version": 1,
            "planning_authority": {
                "sets": [
                    {
                        "id": "domain-a",
                        "members": [
                            {
                                "path": "plans/domain-a.md",
                                "role": "source",
                                "repair": "repairable",
                                "required": True,
                            },
                            {
                                "path": "tasks/domain-a.json",
                                "role": "task_ledger",
                                "repair": "repairable",
                                "required": True,
                            },
                            {
                                "path": "generated/projection.json",
                                "role": "projection",
                                "repair": "generated",
                                "required": True,
                            },
                        ],
                        "validators": [],
                        "reconcilers": [],
                    },
                    {
                        "id": "domain-b",
                        "members": [
                            {
                                "path": "plans/domain-b.md",
                                "role": "source",
                                "repair": "repairable",
                                "required": True,
                            },
                            {
                                "path": "tasks/domain-b.json",
                                "role": "task_ledger",
                                "repair": "repairable",
                                "required": True,
                            },
                        ],
                        "validators": [],
                        "reconcilers": [],
                    },
                ],
            },
            "tasks": {
                "sources": [
                    {
                        "id": "ledger-a",
                        "kind": "json",
                        "authority_sets": ["domain-a", "domain-b"],
                        "paths": ["tasks/domain-a.json"],
                    },
                    {
                        "id": "ledger-b",
                        "kind": "json",
                        "authority_sets": ["domain-b"],
                        "paths": ["tasks/domain-b.json"],
                    },
                ],
                "execution_mode": "single-writer",
                "strict_dependencies": True,
            },
            "control_surfaces": [],
        }
        gov = root / ".claude-auto" / "governance.json"
        gov.parent.mkdir(parents=True, exist_ok=True)
        gov.write_text(json.dumps(governance, indent=2) + "\n")
        _run(root, "git", "add", "-A")
        _run(root, "git", "commit", "-qm", "complex shadow fixture")

        state_root = activate(root)
        task_set = resolve_task_sources(root, persist=True)
        assert task_set["status"] == "READY"
        rows = {row["task"]["id"]: row for row in task_set["tasks"]}
        product_sha = task_set["product_head"]

        accepted = persist_accepted_task_record(
            state_root,
            {
                "schema_version": 1,
                "task_id": "T0",
                "task_spec_sha256": rows["T0"]["task_spec_sha256"],
                "task_source_set_sha256": task_set["task_source_set_sha256"],
                "authority_snapshot_sha256": task_set["authority_snapshot_sha256"],
                "execution_envelope_sha256": "1" * 64,
                "base_sha": product_sha,
                "candidate_sha": product_sha,
                "accepted_product_sha": product_sha,
                "no_op": True,
                "verification_bundle_sha256": "2" * 64,
                "verifier_attestation_sha256": "3" * 64,
                "attestation_contract": "claude-auto/task-acceptance/v1",
            },
        )
        state = load_json(state_root / "state.json", {})
        assert isinstance(state, dict)
        state["accepted_tasks"] = {
            "T0": {
                "task_spec_sha256": rows["T0"]["task_spec_sha256"],
                "accepted_product_sha": product_sha,
                "acceptance_sha256": accepted["acceptance_sha256"],
            }
        }
        json_dump(state_root / "state.json", state)

        workspace = begin_task_workspace(root, task_id="T1")
        assert workspace["task_id"] == "T1"
        assert workspace["lifecycle_state"] == "ACTIVE"

        git_before = _git_truth(root)
        files_before = _state_files(state_root)

        # Build the legacy observation independently from the committed
        # authority/task primitives and known scheduler semantics. Do not call
        # compute_rc4_shadow_observation to produce the expected document.
        snapshot = build_authority_snapshot(root)
        assert snapshot is not None
        independent_task_set = resolve_task_sources(root, persist=False)
        independent_workspace = load_active_task_workspace(
            root,
            state_dir=state_root,
        )
        assert independent_workspace is not None

        def digest(value) -> str:
            return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

        authority_sets = []
        for raw in snapshot["sets"]:
            semantic = {
                "id": raw["id"],
                "members": [
                    {
                        "path": member["path"],
                        "role": member["role"],
                        "repair": member["repair"],
                    }
                    for member in raw["members"]
                ],
                "validators": raw.get("validators") or [],
                "reconcilers": raw.get("reconcilers") or [],
            }
            authority_sets.append({
                **semantic,
                "authority_set_sha256": digest(semantic),
            })
        authority_sets.sort(key=lambda item: item["id"].encode("utf-8"))
        independent_authority = {
            "status": "READY",
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "source_mode": snapshot["source_mode"],
            "set_ids": [item["id"] for item in authority_sets],
            "sets": authority_sets,
            "control_surface_digest": snapshot["control_surface_digest"],
            "protected_paths": list(snapshot["protected_paths"]),
        }

        task_rows = independent_task_set["tasks"]
        by_id = {row["task"]["id"]: row for row in task_rows}

        def task_view(task_id: str):
            row = by_id[task_id]
            task_value = row["task"]
            return {
                "id": task_id,
                "task_spec_sha256": row["task_spec_sha256"],
                "depends_on": list(task_value["depends_on"]),
                "owned_paths": list(task_value["owned_paths"]),
                "evidence_paths": list(task_value["evidence_paths"]),
                "runtime_scratch_paths": list(
                    task_value["runtime_scratch_paths"]
                ),
                "verification_claims": list(task_value["verification"]),
            }

        active_view = {
            **task_view("T1"),
            "lifecycle_state": independent_workspace["lifecycle_state"],
            "task_workspace_sha256": independent_workspace[
                "task_workspace_sha256"
            ],
            "execution_envelope_sha256": independent_workspace[
                "execution_envelope_sha256"
            ],
            "candidate_sha": independent_workspace.get("candidate_sha"),
        }
        legacy = {
            "schema_version": 1,
            "product_sha": product_sha,
            "authority": independent_authority,
            "task_source": {
                "status": independent_task_set["status"],
                "task_source_set_sha256": independent_task_set[
                    "task_source_set_sha256"
                ],
                "merged_tasks_sha256": independent_task_set[
                    "merged_tasks_sha256"
                ],
                "strict_dependencies": independent_task_set[
                    "strict_dependencies"
                ],
                "task_graph": [
                    {
                        "id": row["task"]["id"],
                        "task_spec_sha256": row["task_spec_sha256"],
                        "depends_on": list(row["task"]["depends_on"]),
                    }
                    for row in task_rows
                ],
                "external_dependencies": [],
            },
            "ready_frontier": ["T1"],
            "active_task": active_view,
            "next_task": task_view("T1"),
            "accepted_tasks": {
                "validated_ids": ["T0"],
                "persisted_claims": [{
                    "task_id": "T0",
                    "task_spec_sha256": rows["T0"]["task_spec_sha256"],
                    "accepted_product_sha": product_sha,
                    "acceptance_sha256": accepted["acceptance_sha256"],
                }],
                "invalid_claims": [],
            },
            "blockers": [{
                "task_id": "T2",
                "blockers": [{
                    "dependency": "T1",
                    "reason": "local dependency is not accepted",
                }],
            }],
            "reservations": [],
            "planning_repair": None,
            "verification_through": {
                "candidate_sha": independent_workspace.get("candidate_sha"),
                "verified_candidate_sha": independent_workspace.get(
                    "verified_candidate_sha"
                ),
                "verification_bundle_sha256": independent_workspace.get(
                    "verification_bundle_sha256"
                ),
                "acceptance_attestation_sha256": independent_workspace.get(
                    "acceptance_attestation_sha256"
                ),
            },
        }
        legacy_path = Path(td) / "legacy-complex.json"
        legacy_path.write_text(json.dumps(legacy, indent=2) + "\n")

        comparison = shadow_compare(root, legacy_path)
        assert comparison["status"] == "MATCH"
        assert comparison["comparison"]["mismatch_count"] == 0
        assert comparison["comparison"]["evidence_gap_count"] == 0
        rc4 = comparison["rc4"]
        assert rc4["authority"]["set_ids"] == ["domain-a", "domain-b"]
        assert rc4["accepted_tasks"]["validated_ids"] == ["T0"]
        assert rc4["active_task"]["id"] == "T1"
        assert rc4["blockers"][0]["task_id"] == "T2"
        projection = [
            member
            for authority_set in rc4["authority"]["sets"]
            for member in authority_set["members"]
            if member["path"] == "generated/projection.json"
        ]
        assert projection == [{
            "path": "generated/projection.json",
            "role": "projection",
            "repair": "generated",
        }]
        assert _git_truth(root) == git_before
        files_after = _state_files(state_root)
        added = sorted(set(files_after) - set(files_before))
        assert len(added) == 1
        assert added[0].startswith("shadow/audits/")

        # Use the same complex fixture to qualify bounded legacy-state import.
        # Import is evidence-only: it may write AdoptionRecord files and the
        # legacy_adoption pointer, but it may not mutate product refs or current
        # accepted/active RC4 authority.
        state_before_adoption = load_json(state_root / "state.json", {})
        authority_before = {
            key: state_before_adoption.get(key)
            for key in (
                "accepted_tasks",
                "active_task_id",
                "active_task_spec_sha256",
                "active_execution_envelope_sha256",
            )
        }
        git_before_adoption = _git_truth(root)
        legacy_adoption = Path(td) / "legacy-adoption.json"
        legacy_adoption.write_text(json.dumps({
            "schema_version": 1,
            "source_system": "neutral-legacy-harness",
            "source_state_id": "complex-field-001",
            "product_sha": product_sha,
            "verified_through_sha": product_sha,
            "authority_snapshot_sha256": snapshot["snapshot_sha256"],
            "active_task": {
                "id": "T1",
                "task_spec_sha256": rows["T1"]["task_spec_sha256"],
                "base_sha": product_sha,
            },
            "accepted_tasks": [{
                "id": "T0",
                "task_spec_sha256": rows["T0"]["task_spec_sha256"],
                "accepted_product_sha": product_sha,
            }],
            "blockers": ["T2 waits for T1 acceptance"],
            "reservations": ["legacy-worker-complex"],
        }, indent=2) + "\n")

        adoption = import_legacy_state_claims(root, legacy_adoption)
        assert adoption["legacy_product_relationship"]["status"] == "MATCH"
        assert adoption["legacy_verified_relationship"]["status"] == "MATCH"
        assert adoption["legacy_authority_status"] == "MATCH"
        assert adoption["source_system"] == "neutral-legacy-harness"
        assert adoption["source_state_id"] == "complex-field-001"
        assert _git_truth(root) == git_before_adoption

        state_after_adoption = load_json(state_root / "state.json", {})
        for key, value in authority_before.items():
            assert state_after_adoption.get(key) == value
        assert state_after_adoption["legacy_adoption"]["adoption_sha256"] == (
            adoption["adoption_sha256"]
        )
