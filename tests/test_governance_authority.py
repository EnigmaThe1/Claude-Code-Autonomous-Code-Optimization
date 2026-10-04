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

from authority_set import AuthoritySetError, build_authority_snapshot
from governance_contract import GovernanceContractError, load_governance_contract
from repo_identity import repo_state_dir
from repo_runtime import activate
from settings_policy import make_settings


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
    _run(root, "git", "config", "user.name", "Test")
    _run(root, "git", "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("fixture\n")
    _run(root, "git", "add", "README.md")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _contract(sets: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "planning_authority": {"sets": sets},
        "tasks": {
            "sources": [],
            "execution_mode": "single-writer",
            "strict_dependencies": True,
        },
        "control_surfaces": [],
    }


def _set(set_id: str, members: list[dict]) -> dict:
    return {
        "id": set_id,
        "members": members,
        "validators": [],
        "reconcilers": [],
    }


def _member(path: str, role: str = "source", repair: str = "repairable") -> dict:
    return {"path": path, "role": role, "repair": repair, "required": True}


def _write_contract(root: Path, value: dict) -> None:
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    _run(root, "git", "add", ".claude-auto/governance.json")
    _run(root, "git", "commit", "-qm", "governance")


def test_multi_file_authority_snapshot_is_deterministic(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        (root / "tasks").mkdir()
        (root / "tasks" / "a.json").write_text("{}\n")
        (root / "tasks" / "b.json").write_text("{}\n")
        _run(root, "git", "add", "PLAN.md", "tasks")
        _run(root, "git", "commit", "-qm", "authority")
        _write_contract(root, _contract([
            _set("default", [
                _member("PLAN.md"),
                _member("tasks/*.json", role="task_ledger"),
            ])
        ]))

        first = build_authority_snapshot(root)
        second = build_authority_snapshot(root)
        assert first is not None and second is not None
        assert first["snapshot_sha256"] == second["snapshot_sha256"]
        assert [m["path"] for m in first["sets"][0]["members"]] == [
            "PLAN.md", "tasks/a.json", "tasks/b.json"
        ]


def test_multiple_named_authority_sets_resolve(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        for name in ("platform.md", "service.md"):
            (root / name).write_text(name + "\n")
        _run(root, "git", "add", "platform.md", "service.md")
        _run(root, "git", "commit", "-qm", "plans")
        _write_contract(root, _contract([
            _set("platform", [_member("platform.md")]),
            _set("service-a", [_member("service.md")]),
        ]))
        snap = build_authority_snapshot(root)
        assert snap is not None
        assert [item["id"] for item in snap["sets"]] == ["platform", "service-a"]


def test_legacy_canonical_plan_becomes_one_member_default_set(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        sd = repo_state_dir(root)
        policy = sd / "planning-repair" / "policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({"schema_version": 1, "canonical_plan": "PLAN.md"}))

        snap = build_authority_snapshot(root)
        assert snap is not None
        assert snap["source_mode"] == "legacy"
        assert snap["sets"][0]["id"] == "default"
        assert snap["sets"][0]["members"][0]["path"] == "PLAN.md"


def test_contract_and_incompatible_legacy_policy_fail_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        for name in ("PLAN.md", "OTHER.md"):
            (root / name).write_text(name + "\n")
        _run(root, "git", "add", "PLAN.md", "OTHER.md")
        _run(root, "git", "commit", "-qm", "plans")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        sd = repo_state_dir(root)
        policy = sd / "planning-repair" / "policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({"schema_version": 1, "canonical_plan": "OTHER.md"}))
        with pytest.raises(AuthoritySetError, match="divergent"):
            build_authority_snapshot(root)


def test_contract_duplicate_key_and_traversal_are_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        path = root / ".claude-auto" / "governance.json"
        path.parent.mkdir()
        path.write_text('{"schema_version":1,"schema_version":1}\n')
        _run(root, "git", "add", ".claude-auto/governance.json")
        _run(root, "git", "commit", "-qm", "bad")
        with pytest.raises(GovernanceContractError, match="duplicate JSON key"):
            load_governance_contract(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, _contract([_set("default", [_member("../PLAN.md")])]))
        with pytest.raises(GovernanceContractError, match="must not contain '..'"):
            load_governance_contract(root)


def test_required_zero_match_and_symlink_authority_fail_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, _contract([_set("default", [_member("missing/*.md")])]))
        with pytest.raises(AuthoritySetError, match="zero tracked files"):
            build_authority_snapshot(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "real.md").write_text("real\n")
        os.symlink("real.md", root / "PLAN.md")
        _run(root, "git", "add", "real.md", "PLAN.md")
        _run(root, "git", "commit", "-qm", "symlink")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        with pytest.raises(AuthoritySetError, match="symlink"):
            build_authority_snapshot(root)


def test_authority_wip_divergence_blocks_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        (root / "PLAN.md").write_text("unaccepted change\n")
        with pytest.raises(AuthoritySetError, match="working tree differs"):
            build_authority_snapshot(root)


def test_activation_migrates_state_to_schema_9(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        sd = activate(root)
        obj = json.loads((sd / "state.json").read_text())
        assert obj["schema_version"] >= 9
        assert obj["accepted_tasks"] == {}
        assert obj["governance_snapshot_sha256"] is None
        assert obj["governance_snapshot_generation"] == 0


def test_governance_protection_applies_to_every_profile(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        sd = activate(root)
        repo_profile = {"repo_root": str(root)}
        for profile in ("balanced", "strict", "isolated-full", "unattended"):
            settings = make_settings(sd, "external", profile, repo_profile)
            protected = json.loads(settings["env"]["CLAUDE_AUTO_PROTECTED_REPO_PATHS"])
            assert str((root / "PLAN.md").resolve()) in protected
            assert "Edit(./PLAN.md)" in settings["permissions"]["deny"]
        unattended = make_settings(sd, "external", "unattended", repo_profile)
        assert unattended["sandbox"]["enabled"] is False


def test_governance_snapshot_changes_when_authority_blob_changes(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("one\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        before = build_authority_snapshot(root)
        (root / "PLAN.md").write_text("two\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "change plan")
        after = build_authority_snapshot(root)
        assert before is not None and after is not None
        assert before["snapshot_sha256"] != after["snapshot_sha256"]
