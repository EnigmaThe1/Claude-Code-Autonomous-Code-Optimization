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
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from cli_schema import build_parser
from repo_identity import repo_state_dir
from shadow_validation import (
    MAX_LEGACY_OBSERVATION_BYTES,
    ShadowValidationError,
    compare_shadow_observations,
    compute_rc4_shadow_observation,
    load_legacy_shadow_observation,
    shadow_compare,
)


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
