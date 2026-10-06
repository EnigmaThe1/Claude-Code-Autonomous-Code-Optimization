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
