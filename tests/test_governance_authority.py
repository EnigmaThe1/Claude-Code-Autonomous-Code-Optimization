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

from authority_set import (
    AuthoritySetError,
    authority_content_sha256,
    authority_status,
    build_authority_snapshot,
)
from cli_schema import build_parser as build_cli_parser
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


def test_governance_contract_wip_divergence_blocks_activation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        value = _contract([_set("default", [_member("PLAN.md")])])
        _write_contract(root, value)
        value["tasks"]["strict_dependencies"] = False
        (root / ".claude-auto" / "governance.json").write_text(json.dumps(value) + "\n")
        with pytest.raises(GovernanceContractError, match="differs"):
            load_governance_contract(root)
        with pytest.raises(AuthoritySetError, match="differs"):
            build_authority_snapshot(root)


def test_case_and_unicode_collisions_are_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "Plan.md").write_text("one\n")
        (root / "plan.md").write_text("two\n")
        _run(root, "git", "add", "Plan.md", "plan.md")
        _run(root, "git", "commit", "-qm", "case collision")
        _write_contract(root, _contract([
            _set("default", [_member("*.md")])
        ]))
        with pytest.raises(AuthoritySetError, match="colliding"):
            build_authority_snapshot(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        composed = "\u00e9.md"
        decomposed = "e\u0301.md"
        (root / composed).write_text("one\n")
        (root / decomposed).write_text("two\n")
        _run(root, "git", "add", composed, decomposed)
        _run(root, "git", "commit", "-qm", "unicode collision")
        _write_contract(root, _contract([
            _set("default", [_member("*.md")])
        ]))
        with pytest.raises(AuthoritySetError, match="ambiguously|colliding"):
            build_authority_snapshot(root)


def test_gitlink_is_a_separate_authority_boundary(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        _run(root, "git", "update-index", "--add", "--cacheinfo", f"160000,{head},nested")
        _run(root, "git", "commit", "-qm", "gitlink")
        _write_contract(root, _contract([
            _set("default", [_member("nested")])
        ]))
        with pytest.raises(AuthoritySetError, match="gitlink"):
            build_authority_snapshot(root)


def test_equivalent_legacy_and_governance_authority_are_compatible(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        sd = repo_state_dir(root)
        policy = sd / "planning-repair" / "policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({"schema_version": 1, "canonical_plan": "PLAN.md"}))
        snapshot = build_authority_snapshot(root)
        assert snapshot is not None
        assert snapshot["source_mode"] == "contract+legacy"


def test_control_surface_blob_change_invalidates_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        control = root / ".claude-auto" / "verification.json"
        control.parent.mkdir(parents=True)
        control.write_text('{"commands":["one"]}\n')
        _run(root, "git", "add", "PLAN.md", ".claude-auto/verification.json")
        _run(root, "git", "commit", "-qm", "authority inputs")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        before = build_authority_snapshot(root)
        control.write_text('{"commands":["two"]}\n')
        _run(root, "git", "add", ".claude-auto/verification.json")
        _run(root, "git", "commit", "-qm", "change verification")
        after = build_authority_snapshot(root)
        assert before is not None and after is not None
        assert before["control_surface_digest"] != after["control_surface_digest"]
        assert before["snapshot_sha256"] != after["snapshot_sha256"]


def test_absolute_and_control_character_selectors_are_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, _contract([_set("default", [_member("/PLAN.md")])]))
        with pytest.raises(GovernanceContractError, match="relative"):
            load_governance_contract(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, _contract([_set("default", [_member("bad\nname.md")])]))
        with pytest.raises(GovernanceContractError, match="control"):
            load_governance_contract(root)


def test_oversized_governance_contract_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        path = root / ".claude-auto" / "governance.json"
        path.parent.mkdir(parents=True)
        path.write_text(" " * (2 * 1024 * 1024 + 1))
        _run(root, "git", "add", ".claude-auto/governance.json")
        _run(root, "git", "commit", "-qm", "oversized governance")
        with pytest.raises(GovernanceContractError, match="maximum supported size"):
            load_governance_contract(root)


def test_control_surface_wip_divergence_blocks_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        control = root / ".claude-auto" / "verification.json"
        control.parent.mkdir(parents=True)
        control.write_text('{"commands":["one"]}\n')
        _run(root, "git", "add", "PLAN.md", ".claude-auto/verification.json")
        _run(root, "git", "commit", "-qm", "authority inputs")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        control.write_text('{"commands":["unaccepted"]}\n')
        with pytest.raises(AuthoritySetError, match="working tree differs"):
            build_authority_snapshot(root)


def test_role_mutability_and_helper_contract_changes_invalidate_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        tools = root / "tools"
        tools.mkdir()
        (tools / "check.py").write_text("raise SystemExit(0)\n")
        _run(root, "git", "add", "PLAN.md", "tools/check.py")
        _run(root, "git", "commit", "-qm", "authority inputs")

        helper = {
            "id": "validate-plan",
            "argv": ["python3", "tools/check.py"],
            "cwd": ".",
            "inputs": ["PLAN.md", "tools/check.py"],
            "outputs": [],
            "timeout_seconds": 30,
            "capabilities": {"network": [], "read_external": []},
        }
        value = _contract([_set("default", [_member("PLAN.md")])])
        value["planning_authority"]["sets"][0]["validators"] = [helper]
        _write_contract(root, value)
        first = build_authority_snapshot(root)
        assert first is not None

        changed = json.loads(json.dumps(value))
        member = changed["planning_authority"]["sets"][0]["members"][0]
        member["role"] = "contract"
        member["repair"] = "immutable"
        changed["planning_authority"]["sets"][0]["validators"][0]["timeout_seconds"] = 60
        path = root / ".claude-auto" / "governance.json"
        path.write_text(json.dumps(changed, indent=2) + "\n")
        _run(root, "git", "add", ".claude-auto/governance.json")
        _run(root, "git", "commit", "-qm", "change authority semantics")

        second = build_authority_snapshot(root)
        assert second is not None
        assert second["sets"][0]["members"][0]["role"] == "contract"
        assert second["sets"][0]["members"][0]["repair"] == "immutable"
        assert first["sets"][0]["validator_digest"] != second["sets"][0]["validator_digest"]
        assert first["snapshot_sha256"] != second["snapshot_sha256"]


def test_governance_status_surface_is_read_only_and_parseable(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))
        before = _run(root, "git", "status", "--porcelain=v1").stdout

        args = build_cli_parser("test").parse_args([
            "governance", "status", "--repo", str(root)
        ])
        assert args.command == "governance"
        assert args.governance_command == "status"

        result = authority_status(root)
        assert result["status"] == "READY"
        assert result["snapshot"]["sets"][0]["members"][0]["path"] == "PLAN.md"
        after = _run(root, "git", "status", "--porcelain=v1").stdout
        assert after == before == ""


def test_nested_instructions_ci_and_verification_script_are_control_surfaces(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        (root / "src").mkdir()
        (root / "src" / "CLAUDE.md").write_text("nested instruction\n")
        workflow = root / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: ci\n")
        verify = root / "tools" / "verify.py"
        verify.parent.mkdir()
        verify.write_text("raise SystemExit(0)\n")
        verification = root / ".claude-auto" / "verification.json"
        verification.parent.mkdir(parents=True, exist_ok=True)
        verification.write_text(json.dumps({
            "schema_version": 1,
            "commands": {"test": ["python3 tools/verify.py"]},
        }))
        _run(
            root,
            "git",
            "add",
            "PLAN.md",
            "src/CLAUDE.md",
            ".github/workflows/ci.yml",
            "tools/verify.py",
            ".claude-auto/verification.json",
        )
        _run(root, "git", "commit", "-qm", "control surfaces")
        _write_contract(root, _contract([_set("default", [_member("PLAN.md")])]))

        snapshot = build_authority_snapshot(root)
        assert snapshot is not None
        protected = set(snapshot["protected_paths"])
        assert "src/CLAUDE.md" in protected
        assert ".github/workflows/ci.yml" in protected
        assert "tools/verify.py" in protected
        assert ".claude-auto/verification.json" in protected

        verify.write_text("raise SystemExit(1)\n")
        with pytest.raises(AuthoritySetError, match="working tree differs"):
            build_authority_snapshot(root)


def test_authority_content_digest_is_worktree_independent(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        _write_contract(root, _contract([
            _set("default", [_member("PLAN.md")]),
        ]))

        linked = Path(td) / "linked"
        _run(
            root,
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "authority-content-linked",
            str(linked),
            "HEAD",
        )
        try:
            primary = build_authority_snapshot(root)
            secondary = build_authority_snapshot(linked)
            assert primary is not None and secondary is not None

            # P1 remains intentionally worktree/branch specific.
            assert primary["repository_id"] != secondary["repository_id"]
            assert primary["branch"] != secondary["branch"]
            assert primary["snapshot_sha256"] != secondary["snapshot_sha256"]

            # P5 compares only the semantic planning authority across roots.
            assert authority_content_sha256(primary) == authority_content_sha256(
                secondary
            )
        finally:
            _run(
                root,
                "git",
                "worktree",
                "remove",
                "--force",
                str(linked),
            )
            _run(root, "git", "branch", "-D", "authority-content-linked")


def test_authority_content_digest_changes_with_member_blob(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("one\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan one")
        _write_contract(root, _contract([
            _set("default", [_member("PLAN.md")]),
        ]))

        before = build_authority_snapshot(root)
        assert before is not None
        before_digest = authority_content_sha256(before)

        (root / "PLAN.md").write_text("two\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan two")
        after = build_authority_snapshot(root)
        assert after is not None

        assert authority_content_sha256(after) != before_digest


def test_p7_lfs_pointer_authority_member_fails_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + ("a" * 64) + "\n"
            "size 123456\n"
        )
        (root / "PLAN.md").write_text(pointer)
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "lfs pointer authority")
        _write_contract(root, _contract([
            _set("default", [_member("PLAN.md")])
        ]))
        with pytest.raises(AuthoritySetError, match="Git LFS pointer"):
            build_authority_snapshot(root)
