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
import sys
import tempfile
from pathlib import Path

import pytest

from execution_envelope import (
    ExecutionEnvelopeError,
    evaluate_active_workspace,
    load_active_execution_envelope,
    load_task_violation,
    validate_staged_diff,
)
from repo_identity import repo_state_dir
from repo_runtime import activate
from runtime_paths import package_root
from settings_policy import make_settings
from state_store import json_dump, load_json
from task_authority import (
    TaskAuthorityError,
    active_task_prompt_context,
    activate_task,
    deactivate_task,
    ensure_supervisor_task_activation,
    ready_frontier,
    reconcile_task_authority,
    task_readiness,
)
from task_sources import (
    TaskSourceError,
    load_resolved_task_source_set,
    resolve_task_sources,
)
from workspace_recovery import promote_fast_forward


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WRITE_GUARD = PACKAGE_ROOT / "hooks" / "write_boundary_guard.py"
POST_BATCH_GUARD = PACKAGE_ROOT / "hooks" / "task_post_batch_guard.py"


def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.name", "P3 Test")
    _run(root, "git", "config", "user.email", "p3@example.invalid")
    (root / "PLAN.md").write_text("# Plan\n")
    owned = root / "src" / "task"
    owned.mkdir(parents=True)
    (owned / "existing.txt").write_text("base\n")
    (root / "unrelated.txt").write_text("base unrelated\n")
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _task(
    task_id: str,
    *,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
    evidence_paths: list[str] | None = None,
    scratch_paths: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": ["default"],
        "depends_on": depends_on if depends_on is not None else [],
        "owned_paths": owned_paths if owned_paths is not None else ["src/task/**"],
        "evidence_paths": (
            evidence_paths
            if evidence_paths is not None
            else [f"evidence/{task_id}/**"]
        ),
        "runtime_scratch_paths": (
            scratch_paths
            if scratch_paths is not None
            else [f".scratch/{task_id}/**"]
        ),
        "verification": ["test"],
        "commit_subject": None,
        "metadata": {},
    }


def _contract(tasks: list[dict]) -> dict:
    return {
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


def _write_contract(root: Path, tasks: list[dict]) -> None:
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_contract(tasks), indent=2) + "\n")
    _run(root, "git", "add", ".claude-auto/governance.json")
    _run(root, "git", "commit", "-qm", "governance")


def _configured(root: Path, tasks: list[dict]) -> Path:
    _write_contract(root, tasks)
    result = resolve_task_sources(root, persist=True)
    assert result["status"] == "READY"
    return root


def _hook_env(root: Path, *, semantic_only: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "CLAUDE_AUTO_REPO_ROOT": str(root.resolve()),
        "CLAUDE_AUTONOMY_STATE_DIR": str(repo_state_dir(root)),
        "CLAUDE_AUTO_PACKAGE_ROOT": str(package_root().resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if semantic_only:
        env["CLAUDE_AUTO_SEMANTIC_ONLY_WRITE_GUARD"] = "1"
    else:
        env.pop("CLAUDE_AUTO_SEMANTIC_ONLY_WRITE_GUARD", None)
    return env


def _pretool(root: Path, event: dict, *, semantic_only: bool = False) -> dict:
    cp = subprocess.run(
        [sys.executable, str(WRITE_GUARD)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=_hook_env(root, semantic_only=semantic_only),
        check=True,
    )
    return json.loads(cp.stdout)


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _post_batch(
    root: Path,
    tool_names: list[str],
    *,
    bash_commands: list[str] | None = None,
) -> dict | None:
    bash_commands = list(bash_commands or [])
    bash_index = 0
    calls = []
    for index, name in enumerate(tool_names):
        tool_input = {}
        if name == "Bash" and bash_index < len(bash_commands):
            tool_input = {"command": bash_commands[bash_index]}
            bash_index += 1
        calls.append({
            "tool_name": name,
            "tool_input": tool_input,
            "tool_use_id": f"tool-{index}",
            "tool_response": "omitted",
        })
    event = {
        "session_id": "p3-test-session",
        "cwd": str(root),
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PostToolBatch",
        "tool_calls": calls,
    }
    cp = subprocess.run(
        [sys.executable, str(POST_BATCH_GUARD)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=_hook_env(root),
        check=True,
    )
    return json.loads(cp.stdout) if cp.stdout.strip() else None


def _candidate_commit(root: Path, changes: dict[str, str | None]) -> str:
    with tempfile.TemporaryDirectory() as td:
        worktree = Path(td) / "candidate"
        _run(root, "git", "worktree", "add", "--detach", "-q", str(worktree), "HEAD")
        try:
            for rel, value in changes.items():
                path = worktree / rel
                if value is None:
                    if path.exists() or path.is_symlink():
                        path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            _run(worktree, "git", "add", "-A")
            _run(worktree, "git", "commit", "-qm", "candidate")
            return _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        finally:
            _run(root, "git", "worktree", "remove", "--force", str(worktree), check=False)


def test_root_task_activation_and_dependency_frontier(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(
            _repo(Path(td) / "repo"),
            [_task("T1"), _task("T2", depends_on=["T1"])],
        )
        assert ready_frontier(root) == ["T1"]
        result = activate_task(root, "T1")
        assert result["status"] == "ACTIVE"
        assert result["task_id"] == "T1"
        envelope = load_active_execution_envelope(root)
        assert envelope["task_id"] == "T1"
        assert envelope["product_base_sha"] == _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        state_obj = load_json(repo_state_dir(root) / "state.json", {})
        assert state_obj["active_task_id"] == "T1"
        assert state_obj["active_task_spec_sha256"] == envelope["task_spec_sha256"]
        assert state_obj["active_execution_envelope_sha256"] == envelope["execution_envelope_sha256"]
        with pytest.raises(TaskAuthorityError, match="already active"):
            activate_task(root, "T2")


def test_dependency_acceptance_requires_current_taskspec_and_ancestor(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(
            _repo(Path(td) / "repo"),
            [_task("T1"), _task("T2", depends_on=["T1"])],
        )
        task_set = load_resolved_task_source_set(root)
        rows = {row["task"]["id"]: row for row in task_set["tasks"]}
        state_path = repo_state_dir(root) / "state.json"
        state_obj = load_json(state_path, {})
        state_obj["accepted_tasks"] = {
            "T1": {
                "task_spec_sha256": rows["T1"]["task_spec_sha256"],
                "accepted_product_sha": task_set["product_head"],
            }
        }
        json_dump(state_path, state_obj)
        assert task_readiness(root)["T2"]["status"] == "READY"
        assert ready_frontier(root) == ["T2"]

        state_obj = load_json(state_path, {})
        state_obj["accepted_tasks"]["T1"]["task_spec_sha256"] = "0" * 64
        json_dump(state_path, state_obj)
        status = task_readiness(root)["T2"]
        assert status["status"] == "BLOCKED"
        assert "different TaskSpec digest" in status["blockers"][0]["reason"]


def test_activation_rejects_staged_and_overlapping_wip_but_preserves_unrelated(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])

        (root / "unrelated-wip.txt").write_text("keep me\n")
        result = activate_task(root, "T1")
        assert result["status"] == "ACTIVE"
        (root / "src" / "task" / "new.txt").write_text("task\n")
        assert evaluate_active_workspace(root)["status"] == "VALID"
        (root / "unrelated-wip.txt").write_text("changed\n")
        evaluated = evaluate_active_workspace(root)
        assert evaluated["status"] == "VIOLATION"
        assert any("pre-existing WIP changed" in row["reason"] for row in evaluated["violations"])

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        (root / "src" / "task" / "owned-wip.txt").write_text("preexisting\n")
        with pytest.raises(TaskAuthorityError, match="overlaps selected task authority"):
            activate_task(root, "T1")

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        (root / "unrelated.txt").write_text("staged\n")
        _run(root, "git", "add", "unrelated.txt")
        with pytest.raises(TaskAuthorityError, match="empty index"):
            activate_task(root, "T1")


def test_direct_and_bash_guard_enforce_task_paths_including_unattended(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as outside_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")

        def direct(path: str, *, semantic_only: bool = False) -> str:
            return _decision(_pretool(root, {
                "tool_name": "Write",
                "tool_input": {"file_path": path},
            }, semantic_only=semantic_only))

        assert direct(str(root / "src" / "task" / "ok.txt")) == "allow"
        assert direct(str(root / "evidence" / "T1" / "proof.txt")) == "allow"
        assert direct(str(root / ".scratch" / "T1" / "direct.txt")) == "deny"
        assert direct(str(root / "outside-task.txt")) == "deny"
        assert direct(str(root / "PLAN.md")) == "deny"

        assert _decision(_pretool(root, {
            "tool_name": "Bash",
            "tool_input": {"command": "touch src/task/bash-ok.txt"},
        })) == "allow"
        assert _decision(_pretool(root, {
            "tool_name": "Bash",
            "tool_input": {"command": "mkdir -p .scratch/T1/build"},
        })) == "allow"
        assert _decision(_pretool(root, {
            "tool_name": "Bash",
            "tool_input": {"command": "touch not-owned.txt"},
        })) == "deny"
        assert _decision(_pretool(root, {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m forbidden"},
        })) == "deny"

        outside = Path(outside_td) / "host-output.txt"
        assert direct(str(outside), semantic_only=True) == "allow"
        state_file = repo_state_dir(root) / "state.json"
        assert direct(str(state_file), semantic_only=True) == "deny"


def test_task_owned_unresolved_repository_denies_product_write(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, [_task("T1")])
        activate(root)
        result = _pretool(root, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(root / "src" / "task" / "x.txt")},
        })
        assert _decision(result) == "deny"
        assert "ExecutionEnvelope" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_post_batch_allows_owned_and_scratch_then_blocks_escape_and_reconciles(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")

        (root / "src" / "task" / "opaque.txt").write_text("allowed\n")
        assert _post_batch(root, ["Bash"]) is None

        scratch = root / ".scratch" / "T1" / "cache.bin"
        scratch.parent.mkdir(parents=True)
        scratch.write_bytes(b"scratch")
        assert _post_batch(root, ["Bash"]) is None

        escaped = root / "escape.txt"
        escaped.write_text("bad\n")
        blocked = _post_batch(root, ["Bash"])
        assert blocked is not None
        assert blocked["decision"] == "block"
        violation = load_task_violation(root)
        assert violation is not None
        assert any(row["path"] == "escape.txt" for row in violation["violations"])

        escaped.unlink()
        result = reconcile_task_authority(root)
        assert result["status"] == "RECONCILED"
        assert load_task_violation(root) is None


def test_post_batch_detects_changed_unrelated_baseline_wip(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        baseline = root / "notes.local"
        baseline.write_text("one\n")
        activate_task(root, "T1")
        baseline.write_text("two\n")
        blocked = _post_batch(root, ["Bash"])
        assert blocked and blocked["decision"] == "block"
        violation = load_task_violation(root)
        assert any("pre-existing WIP changed" in row["reason"] for row in violation["violations"])


def test_stage_gate_accepts_owned_and_rejects_scratch_outside_and_rename(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")

        allowed = root / "src" / "task" / "stage.txt"
        allowed.write_text("allowed\n")
        _run(root, "git", "add", "src/task/stage.txt")
        assert validate_staged_diff(root)["status"] == "VALID"
        _run(root, "git", "reset", "-q", "HEAD", "--", "src/task/stage.txt")
        allowed.unlink()

        scratch = root / ".scratch" / "T1" / "stage.bin"
        scratch.parent.mkdir(parents=True)
        scratch.write_bytes(b"x")
        _run(root, "git", "add", "-f", ".scratch/T1/stage.bin")
        with pytest.raises(ExecutionEnvelopeError, match="scratch"):
            validate_staged_diff(root)
        _run(root, "git", "reset", "-q", "HEAD", "--", ".scratch/T1/stage.bin")
        scratch.unlink()

        outside = root / "outside-stage.txt"
        outside.write_text("bad\n")
        _run(root, "git", "add", "outside-stage.txt")
        with pytest.raises(ExecutionEnvelopeError, match="outside the active task"):
            validate_staged_diff(root)
        _run(root, "git", "reset", "-q", "HEAD", "--", "outside-stage.txt")
        outside.unlink()

        _run(root, "git", "mv", "src/task/existing.txt", "moved-outside.txt")
        with pytest.raises(ExecutionEnvelopeError, match="moved-outside.txt"):
            validate_staged_diff(root)


def test_deactivate_requires_exact_activation_baseline(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        path = root / "src" / "task" / "delta.txt"
        path.write_text("task\n")
        with pytest.raises(TaskAuthorityError, match="activation-baseline"):
            deactivate_task(root)
        path.unlink()
        result = deactivate_task(root)
        assert result["status"] == "INACTIVE"
        state_obj = load_json(repo_state_dir(root) / "state.json", {})
        assert state_obj["active_task_id"] is None


def test_promotion_target_gate_and_head_change_invalidate_task_authority(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        target = _candidate_commit(root, {"src/task/promoted.txt": "ok\n"})
        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert _run(root, "git", "rev-parse", "HEAD").stdout.strip() == target
        state_obj = load_json(repo_state_dir(root) / "state.json", {})
        assert state_obj["active_task_id"] is None
        assert state_obj["active_execution_envelope_sha256"] is None
        assert state_obj["task_source_sha256"] is None

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        target = _candidate_commit(root, {"outside-promotion.txt": "bad\n"})
        before = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        with pytest.raises(ValueError, match="promotion target violates"):
            promote_fast_forward(root, target)
        assert _run(root, "git", "rev-parse", "HEAD").stdout.strip() == before
        assert load_active_execution_envelope(root)["task_id"] == "T1"

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        target = _candidate_commit(root, {".scratch/T1/bad.bin": "bad\n"})
        with pytest.raises(ValueError, match="scratch"):
            promote_fast_forward(root, target)


def test_task_owned_promotion_without_envelope_is_refused(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        target = _candidate_commit(root, {"src/task/no-envelope.txt": "bad\n"})
        with pytest.raises(ValueError, match="requires a current active ExecutionEnvelope"):
            promote_fast_forward(root, target)


def test_post_batch_without_envelope_blocks_mutating_tool_in_task_owned_mode(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        blocked = _post_batch(root, ["Bash"])
        assert blocked and blocked["decision"] == "block"
        assert load_task_violation(root) is not None


def test_envelope_semantic_tamper_and_state_binding_mismatch_fail_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        path = repo_state_dir(root) / "tasks" / "execution-envelope.json"
        raw = json.loads(path.read_text())
        raw["direct_edit_paths"] = ["**"]
        path.write_text(json.dumps(raw))
        with pytest.raises(ExecutionEnvelopeError, match="semantic integrity"):
            load_active_execution_envelope(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        state_path = repo_state_dir(root) / "state.json"
        state_obj = load_json(state_path, {})
        state_obj["active_execution_envelope_sha256"] = "f" * 64
        json_dump(state_path, state_obj)
        with pytest.raises(ExecutionEnvelopeError, match="semantic integrity|durable state"):
            load_active_execution_envelope(root)


def test_post_batch_head_movement_blocks_and_preserves_original_envelope_identity(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activated = activate_task(root, "T1")
        base = activated["product_base_sha"]
        _run(root, "git", "commit", "--allow-empty", "-qm", "out-of-band head move")
        moved = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        assert moved != base

        blocked = _post_batch(root, ["Bash"])
        assert blocked and blocked["decision"] == "block"
        violation = load_task_violation(root)
        assert violation is not None
        assert violation["task_id"] == "T1"
        assert violation["product_base_sha"] == base
        assert violation["observed_head"] == moved
        assert violation["execution_envelope_sha256"] == activated["execution_envelope_sha256"]


def test_unattended_symlink_traversal_is_denied_by_task_authority(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as outside_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        link = root / "src" / "task" / "link"
        link.symlink_to(Path(outside_td), target_is_directory=True)

        result = _pretool(root, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(link / "escape.txt")},
        }, semantic_only=True)
        assert _decision(result) == "deny"

        result = _pretool(root, {
            "tool_name": "Bash",
            "tool_input": {"command": "touch src/task/link/escape.txt"},
        }, semantic_only=True)
        assert _decision(result) == "deny"


def test_gitlink_owned_selector_is_blocked_at_activation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        _run(
            root,
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},nested",
        )
        _run(root, "git", "commit", "-qm", "add synthetic gitlink")
        _write_contract(
            root,
            [_task("T1", owned_paths=["nested/**"], evidence_paths=[], scratch_paths=[])],
        )
        result = resolve_task_sources(root, persist=True)
        assert result["status"] == "READY"
        with pytest.raises(TaskAuthorityError, match="gitlink/submodule"):
            activate_task(root, "T1")


def test_detached_head_and_sparse_checkout_block_activation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        _run(root, "git", "checkout", "--detach", "-q")
        # Detaching changes AuthoritySet identity, so refresh the read-only
        # TaskSourceSet first; activation must still refuse a mutating task on
        # detached HEAD rather than inventing a durable branch destination.
        refreshed = resolve_task_sources(root, persist=True)
        assert refreshed["status"] == "READY"
        with pytest.raises(TaskAuthorityError, match="named branch"):
            activate_task(root, "T1")

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        _run(root, "git", "config", "core.sparseCheckout", "true")
        with pytest.raises(TaskAuthorityError, match="sparse"):
            activate_task(root, "T1")


def test_stage_gate_handles_deletion_mode_only_and_binary_changes(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")

        _run(root, "git", "rm", "-q", "src/task/existing.txt")
        assert validate_staged_diff(root)["status"] == "VALID"
        _run(root, "git", "reset", "--hard", "-q", "HEAD")

        existing = root / "src" / "task" / "existing.txt"
        existing.chmod(existing.stat().st_mode | 0o111)
        _run(root, "git", "add", "src/task/existing.txt")
        assert validate_staged_diff(root)["status"] == "VALID"
        _run(root, "git", "reset", "--hard", "-q", "HEAD")

        binary = root / "src" / "task" / "binary.dat"
        binary.write_bytes(bytes(range(256)))
        _run(root, "git", "add", "src/task/binary.dat")
        assert validate_staged_diff(root)["status"] == "VALID"


def test_settings_install_post_batch_guard_for_every_runtime_profile(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        profile = {"repo_root": str(root), "languages": [], "container_files": []}
        for name in ("balanced", "strict", "isolated-full", "unattended"):
            rendered = make_settings(Path(state), "external", name, profile)
            post_groups = rendered.get("hooks", {}).get("PostToolBatch", [])
            post_hooks = [
                hook
                for group in post_groups
                for hook in group.get("hooks", [])
                if "task_post_batch_guard.py" in hook.get("command", "")
            ]
            assert len(post_hooks) == 1, name
            assert post_hooks[0]["timeout"] == 60

            pre_groups = rendered.get("hooks", {}).get("PreToolUse", [])
            write_hooks = [
                hook
                for group in pre_groups
                for hook in group.get("hooks", [])
                if "write_boundary_guard.py" in hook.get("command", "")
            ]
            assert len(write_hooks) == 1, name
            assert write_hooks[0]["timeout"] == 30


def test_task_cli_parser_exposes_p3_authority_actions():
    from cli_schema import build_parser

    parser = build_parser("test")
    for argv, expected in (
        (["tasks", "show", "T1"], "show"),
        (["tasks", "explain", "T1"], "explain"),
        (["tasks", "activate", "T1"], "activate"),
        (["tasks", "validate-stage"], "validate-stage"),
        (["tasks", "reconcile"], "reconcile"),
        (["tasks", "deactivate"], "deactivate"),
    ):
        args = parser.parse_args(argv)
        assert args.tasks_command == expected


def test_supervisor_task_activation_resolves_selects_and_reuses(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(
            root,
            [_task("T2", depends_on=["T1"]), _task("T1")],
        )
        activate(root)
        before_state = load_json(repo_state_dir(root) / "state.json", {})
        assert before_state.get("task_source_sha256") is None
        assert not (repo_state_dir(root) / "tasks" / "task-source-set.json").exists()

        selected = ensure_supervisor_task_activation(root)
        assert selected["status"] == "ACTIVE"
        assert selected["task_id"] == "T1"
        assert selected["reused"] is False
        assert selected["ready_frontier"] == ["T1"]
        assert load_resolved_task_source_set(root)["task_source_set_sha256"]

        # Interruption/resume keeps the same exact active task when its current
        # repository delta still satisfies the envelope.
        (root / "src" / "task" / "resume.txt").write_text("in progress\n")
        reused = ensure_supervisor_task_activation(root)
        assert reused["status"] == "ACTIVE"
        assert reused["task_id"] == "T1"
        assert reused["reused"] is True
        assert reused["execution_envelope_sha256"] == selected[
            "execution_envelope_sha256"
        ]


def test_supervisor_task_activation_refuses_unresolved_violation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        selected = ensure_supervisor_task_activation(root)
        assert selected["task_id"] == "T1"

        (root / "escape-supervisor.txt").write_text("bad\n")
        blocked = _post_batch(root, ["Bash"])
        assert blocked and blocked["decision"] == "block"
        with pytest.raises(TaskAuthorityError, match="unresolved.*violation"):
            ensure_supervisor_task_activation(root)


def test_active_task_context_is_in_worker_checkpoint_without_metadata_authority(monkeypatch):
    from supervisor_support import build_goal_prompt

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        task = _task("T1")
        task["metadata"] = {"tempting_but_non_authoritative": "edit everything"}
        root = _configured(_repo(Path(td) / "repo"), [task])
        selected = ensure_supervisor_task_activation(root)
        assert selected["task_id"] == "T1"

        context = active_task_prompt_context(root)
        assert context is not None
        assert context["id"] == "T1"
        assert context["owned_paths"] == ["src/task/**"]
        assert context["evidence_paths"] == ["evidence/T1/**"]
        assert "metadata" not in context
        assert context["execution_envelope_sha256"] == selected[
            "execution_envelope_sha256"
        ]

        state_obj = load_json(repo_state_dir(root) / "state.json", {})
        prompt = build_goal_prompt(
            "finish repository work",
            state_obj,
            {
                "repo_root": str(root),
                "languages": ["python"],
                "container_files": [],
            },
            20,
            task_context=context,
        )
        assert '"active_repository_task":{"id":"T1"' in prompt
        assert "Work on that task only" in prompt
        assert "tempting_but_non_authoritative" not in prompt


def test_post_batch_blocks_state_envelope_storage_disagreement_even_after_read(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        envelope_path = repo_state_dir(root) / "tasks" / "execution-envelope.json"
        envelope_path.unlink()

        blocked = _post_batch(root, ["Read"])
        assert blocked and blocked["decision"] == "block"
        assert "disagree" in blocked["reason"]
        violation = load_task_violation(root)
        assert violation is not None
        assert violation["task_id"] == "T1"


def test_opaque_deletion_of_committed_governance_cannot_disable_task_owned_mode(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        governance = root / ".claude-auto" / "governance.json"
        governance.unlink()

        blocked = _post_batch(root, ["Bash"])
        assert blocked and blocked["decision"] == "block"
        assert "governance" in blocked["reason"].lower()
        assert load_task_violation(root) is not None


def test_post_batch_recognises_only_exact_validated_promote_ff_handoff(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        target = _candidate_commit(root, {"src/task/promoted-by-hook.txt": "ok\n"})

        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        handoff = _post_batch(
            root,
            ["Bash"],
            bash_commands=[f"claude-auto promote-ff --repo . --sha {target}"],
        )
        assert handoff is not None
        assert "decision" not in handoff
        context = handoff["hookSpecificOutput"]["additionalContext"]
        assert "intentionally invalidated" in context
        assert "AUTONOMY_STATUS: CONTINUE" in context
        assert load_task_violation(root) is None

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")
        target = _candidate_commit(root, {"src/task/promoted-compound.txt": "ok\n"})
        promote_fast_forward(root, target)

        blocked = _post_batch(
            root,
            ["Bash"],
            bash_commands=[
                f"touch unrelated-after.txt && claude-auto promote-ff --repo . --sha {target}"
            ],
        )
        assert blocked is not None
        assert blocked.get("decision") == "block"
        assert load_task_violation(root) is not None


def test_linked_worktree_gets_distinct_task_authority_state(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        linked = Path(td) / "linked"
        _run(
            root,
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "p3-linked-task",
            str(linked),
            "HEAD",
        )
        try:
            resolved = resolve_task_sources(linked, persist=True)
            assert resolved["status"] == "READY"
            selected = activate_task(linked, "T1")
            assert selected["status"] == "ACTIVE"
            assert selected["task_id"] == "T1"
            assert repo_state_dir(linked) != repo_state_dir(root)
            assert load_active_execution_envelope(linked)["product_base_sha"] == _run(
                linked, "git", "rev-parse", "HEAD"
            ).stdout.strip()
        finally:
            _run(
                root,
                "git",
                "worktree",
                "remove",
                "--force",
                str(linked),
                check=False,
            )


def test_shallow_clone_missing_accepted_history_blocks_dependency_readiness(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        source = _repo(Path(td) / "source")
        old_product = _run(source, "git", "rev-parse", "HEAD").stdout.strip()
        _write_contract(
            source,
            [_task("T1"), _task("T2", depends_on=["T1"])],
        )
        (source / "tip.txt").write_text("tip\n")
        _run(source, "git", "add", "tip.txt")
        _run(source, "git", "commit", "-qm", "shallow tip")

        shallow = Path(td) / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "-q",
                source.resolve().as_uri(),
                str(shallow),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        assert _run(
            shallow,
            "git",
            "cat-file",
            "-e",
            f"{old_product}^{{commit}}",
            check=False,
        ).returncode != 0

        resolved = resolve_task_sources(shallow, persist=True)
        rows = {row["task"]["id"]: row for row in resolved["tasks"]}
        state_path = repo_state_dir(shallow) / "state.json"
        state_obj = load_json(state_path, {})
        state_obj["accepted_tasks"] = {
            "T1": {
                "task_spec_sha256": rows["T1"]["task_spec_sha256"],
                "accepted_product_sha": old_product,
            }
        }
        json_dump(state_path, state_obj)
        readiness = task_readiness(shallow)
        assert readiness["T2"]["status"] == "BLOCKED"
        assert "unavailable in local Git history" in readiness["T2"]["blockers"][0]["reason"]


def test_missing_committed_governance_object_blocks_task_resolution(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_contract(root, [_task("T1")])
        blob = _run(
            root,
            "git",
            "rev-parse",
            "HEAD:.claude-auto/governance.json",
        ).stdout.strip()
        object_path = root / ".git" / "objects" / blob[:2] / blob[2:]
        assert object_path.is_file()
        object_path.unlink()

        with pytest.raises(TaskSourceError, match="governance|blob|activate"):
            resolve_task_sources(root, persist=True)


def test_stage_gate_accepts_unicode_spaces_and_leading_dash_inside_envelope(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        activate_task(root, "T1")

        first = root / "src" / "task" / "space Δ name.bin"
        second = root / "src" / "task" / "-leading-dash.txt"
        first.write_bytes(b"\x00\x01p3")
        second.write_text("dash\n")
        _run(
            root,
            "git",
            "add",
            "--",
            "src/task/space Δ name.bin",
            "src/task/-leading-dash.txt",
        )
        result = validate_staged_diff(root)
        assert result["status"] == "VALID"
        staged_paths = {
            path
            for entry in result["staged_entries"]
            for path in entry["paths"]
        }
        assert "src/task/space Δ name.bin" in staged_paths
        assert "src/task/-leading-dash.txt" in staged_paths


def test_large_monorepo_selector_envelope_remains_deterministic(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        selectors = [f"packages/p{index:03d}/**" for index in range(400)]
        task = _task(
            "T1",
            owned_paths=selectors,
            evidence_paths=[],
            scratch_paths=[],
        )
        root = _configured(_repo(Path(td) / "repo"), [task])
        selected = activate_task(root, "T1")
        envelope = load_active_execution_envelope(root)
        assert selected["status"] == "ACTIVE"
        assert envelope["direct_edit_paths"] == sorted(selectors)

        target = root / "packages" / "p399" / "feature.py"
        target.parent.mkdir(parents=True)
        target.write_text("VALUE = 1\n")
        _run(root, "git", "add", "--", "packages/p399/feature.py")
        assert validate_staged_diff(root)["status"] == "VALID"


def test_linked_task_root_can_use_primary_coordinator_state(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        primary = _configured(_repo(Path(td) / "repo"), [_task("T1")])
        coordinator_state = repo_state_dir(primary)
        linked = Path(td) / "task-linked"
        _run(
            primary,
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "p4-coordinator-state",
            str(linked),
            "HEAD",
        )
        try:
            linked_state = repo_state_dir(linked)
            assert linked_state != coordinator_state
            assert not (linked_state / "tasks" / "execution-envelope.json").exists()

            selected = activate_task(
                linked,
                "T1",
                acquire_lease=False,
                state_dir=coordinator_state,
            )
            assert selected["status"] == "ACTIVE"
            assert selected["task_id"] == "T1"

            envelope = load_active_execution_envelope(
                linked,
                state_dir=coordinator_state,
            )
            assert envelope["task_id"] == "T1"
            assert envelope["product_base_sha"] == _run(
                linked,
                "git",
                "rev-parse",
                "HEAD",
            ).stdout.strip()

            context = active_task_prompt_context(
                linked,
                state_dir=coordinator_state,
            )
            assert context is not None
            assert context["id"] == "T1"
            assert context["execution_envelope_sha256"] == envelope[
                "execution_envelope_sha256"
            ]

            durable = load_json(coordinator_state / "state.json", {})
            assert durable["active_task_id"] == "T1"
            assert durable["active_execution_envelope_sha256"] == envelope[
                "execution_envelope_sha256"
            ]
            assert not (linked_state / "tasks" / "execution-envelope.json").exists()
            assert not (linked_state / "state.json").exists()
        finally:
            _run(
                primary,
                "git",
                "worktree",
                "remove",
                "--force",
                str(linked),
                check=False,
            )
