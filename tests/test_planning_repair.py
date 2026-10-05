from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import planning_repair as pr
from planning_repair import (
    PLANNING_REPAIR_CONTRACT,
    begin_planning_repair,
    configure_planning_repair,
    load_active_repair,
    promote_planning_repair,
    refresh_planning_repair_base,
    run_planning_repair_architect,
    verify_planning_repair,
)
from promotion_policy import load_promotion_attestation
from repair_envelope import (
    RepairEnvelopeError,
    derive_repair_envelope,
    load_repair_envelope,
    persist_repair_envelope,
)
from settings_policy import make_settings
from state_store import json_dump


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks" / "planning_repair_guard.py"
WRITE_GUARD = ROOT / "hooks" / "write_boundary_guard.py"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n\n1. Build the requested product.\n")
    (path / "app.txt").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _hook(tool: str, payload: dict, env: dict[str, str]) -> dict:
    merged = os.environ.copy()
    merged.update(env)
    cp = subprocess.run(
        [os.sys.executable, str(GUARD)],
        input=json.dumps({"tool_name": tool, "tool_input": payload}),
        text=True,
        capture_output=True,
        env=merged,
        check=True,
    )
    return json.loads(cp.stdout)["hookSpecificOutput"]


def _make_candidate(root: Path) -> tuple[dict, str]:
    active = begin_planning_repair(root, reason="repair ordering")
    worktree = Path(active["worktree"])
    plan = worktree / active["canonical_plan"]
    plan.write_text(plan.read_text() + "\n2. Verify the requested product.\n")
    _git(worktree, "add", active["canonical_plan"])
    _git(worktree, "commit", "-qm", "repair plan")
    candidate = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    active = load_active_repair(root)
    active["candidate_sha"] = candidate
    active["verified_sha"] = None
    json_dump(pr._active_path(root), active)
    return active, candidate


def test_configured_plan_is_protected_from_balanced_product_worker(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        settings = make_settings(
            Path(state_td) / "repos" / "placeholder",
            "external",
            "balanced",
            {"repo_root": str(root), "languages": [], "container_files": []},
        )
        # make_settings consumes the state_dir passed to it; use the real repo state
        # for the policy-backed assertion.
        settings = make_settings(
            pr.repo_state_dir(root),
            "external",
            "balanced",
            {"repo_root": str(root), "languages": [], "container_files": []},
        )
        protected = str((root / "IMPLEMENTATION_PLAN.md").resolve())
        assert json.loads(settings["env"]["CLAUDE_AUTO_PROTECTED_REPO_PATHS"]) == [protected]
        deny = settings["permissions"]["deny"]
        assert "Edit(./IMPLEMENTATION_PLAN.md)" in deny
        assert "Write(./IMPLEMENTATION_PLAN.md)" in deny


def test_planning_architect_guard_allows_only_exact_plan_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "worktree"
        root.mkdir()
        plan = root / "IMPLEMENTATION_PLAN.md"
        plan.write_text("plan\n")
        other = root / "app.py"
        other.write_text("x\n")
        env = {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(root),
            "CLAUDE_AUTO_PLAN_REPAIR_PATH": str(plan),
        }
        assert _hook("Edit", {"file_path": str(plan)}, env)["permissionDecision"] == "allow"
        assert _hook("Write", {"file_path": str(other)}, env)["permissionDecision"] == "deny"
        assert _hook("Bash", {"command": "echo x"}, env)["permissionDecision"] == "deny"


def test_architect_creates_plan_only_candidate_commit(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        begin_planning_repair(root, reason="missing verification step")

        monkeypatch.setattr(pr, "provider_from_args", lambda _args: (os.environ.copy(), {"provider": "native"}))

        def fake_control_model(**kwargs):
            worktree = kwargs["root"]
            plan = worktree / "IMPLEMENTATION_PLAN.md"
            plan.write_text(plan.read_text() + "\n2. Add required verification.\n")
            cp = subprocess.CompletedProcess(["claude"], 0, "", "")
            text = (
                'PLANNING_REPAIR_ARCHITECT: '
                '{"verdict":"READY","classification":"PLAN_PRESERVING",'
                '"summary":"adds missing verification without changing product scope"}'
            )
            return cp, text, "architect-session", {}, "SUCCESS", "ok", {}, [], 0.1

        monkeypatch.setattr(pr, "_run_control_model", fake_control_model)
        args = SimpleNamespace(
            reason="missing verification step",
            model=None,
            max_budget_usd=None,
            max_turns=20,
            timeout=0,
        )
        result = run_planning_repair_architect(root, args)
        assert result["status"] == "candidate"
        active = load_active_repair(root)
        assert active["candidate_sha"] == result["candidate_sha"]
        assert pr._changed_paths(Path(active["worktree"]), active["base_sha"], active["candidate_sha"]) == {
            "IMPLEMENTATION_PLAN.md"
        }


def test_independent_verifier_attests_exact_candidate_and_promotion_consumes_it(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        active, candidate = _make_candidate(root)

        monkeypatch.setattr(pr, "provider_from_args", lambda _args: (os.environ.copy(), {"provider": "native"}))

        def fake_verifier(**kwargs):
            return (
                'PLANNING_REPAIR_VERIFY: {"verdict":"VERIFIED","summary":"exact plan SHA is coherent","findings":[]}',
                {
                    "repository_unchanged": True,
                    "git_after": {"head": candidate},
                    "usage": {},
                },
            )

        monkeypatch.setattr(pr, "run_readonly_plan_agent", fake_verifier)
        args = SimpleNamespace(
            sha=candidate,
            model=None,
            timeout=0,
            max_turns=20,
            max_budget_usd=None,
        )
        verified = verify_planning_repair(root, args)
        assert verified["status"] == "verified"
        att = load_promotion_attestation(root, candidate, PLANNING_REPAIR_CONTRACT)
        assert att["target_sha"] == candidate
        assert att["verdict"] == "VERIFIED"

        promoted = promote_planning_repair(root)
        assert promoted["head"] == candidate
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == candidate
        assert load_active_repair(root) == {}
        assert not Path(active["worktree"]).exists()


def test_refresh_base_rebases_plan_only_candidate_and_invalidates_old_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        active, candidate_before = _make_candidate(root)

        (root / "app.txt").write_text("product advanced\n")
        _git(root, "add", "app.txt")
        _git(root, "commit", "-qm", "advance product")
        new_base = _git(root, "rev-parse", "HEAD").stdout.strip()

        result = refresh_planning_repair_base(root)
        assert result["status"] == "rebased"
        assert result["base_sha"] == new_base
        assert result["candidate_sha"] != candidate_before
        active_after = load_active_repair(root)
        assert active_after["verified_sha"] is None
        assert pr._changed_paths(
            Path(active_after["worktree"]), new_base, active_after["candidate_sha"]
        ) == {"IMPLEMENTATION_PLAN.md"}


def test_refresh_base_recognises_candidate_that_already_absorbed_new_product_base(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        active, _candidate = _make_candidate(root)
        worktree = Path(active["worktree"])

        (root / "app.txt").write_text("product advanced\n")
        _git(root, "add", "app.txt")
        _git(root, "commit", "-qm", "advance product")
        new_base = _git(root, "rev-parse", "HEAD").stdout.strip()

        # Simulate an earlier refresh completing but its response/state update being lost.
        _git(worktree, "rebase", "--onto", new_base, active["base_sha"], active["repair_branch"])
        result = refresh_planning_repair_base(root)
        assert result["status"] == "already-absorbed"
        assert result["base_sha"] == new_base


def test_refresh_base_recovers_stale_in_progress_marker_and_retries(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        active, candidate_before = _make_candidate(root)

        (root / "app.txt").write_text("product advanced\n")
        _git(root, "add", "app.txt")
        _git(root, "commit", "-qm", "advance product")
        new_base = _git(root, "rev-parse", "HEAD").stdout.strip()

        active = load_active_repair(root)
        active["refresh"] = {
            "in_progress": True,
            "old_base": active["base_sha"],
            "new_base": new_base,
            "candidate_before": candidate_before,
        }
        json_dump(pr._active_path(root), active)

        result = refresh_planning_repair_base(root)
        assert result["status"] == "rebased"
        assert result["base_sha"] == new_base


def test_product_write_guard_denies_bash_and_direct_mutation_of_canonical_plan(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        settings = make_settings(
            pr.repo_state_dir(root),
            "external",
            "balanced",
            {"repo_root": str(root), "languages": [], "container_files": []},
        )
        env = os.environ.copy()
        env.update(settings["env"])

        direct = subprocess.run(
            [os.sys.executable, str(WRITE_GUARD)],
            input=json.dumps({
                "tool_name": "Edit",
                "tool_input": {"file_path": str(root / "IMPLEMENTATION_PLAN.md")},
            }),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert json.loads(direct.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

        bash = subprocess.run(
            [os.sys.executable, str(WRITE_GUARD)],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "printf 'changed\\n' > IMPLEMENTATION_PLAN.md"},
            }),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert json.loads(bash.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stale_generated_settings_still_obey_new_durable_plan_policy(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)

        # Simulate settings generated before repository-owned planning was enabled.
        stale = make_settings(
            pr.repo_state_dir(root),
            "external",
            "balanced",
            {"repo_root": str(root), "languages": [], "container_files": []},
        )
        assert "CLAUDE_AUTO_PROTECTED_REPO_PATHS" not in stale["env"]

        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        env = os.environ.copy()
        env.update(stale["env"])

        cp = subprocess.run(
            [os.sys.executable, str(WRITE_GUARD)],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "printf 'changed\\n' > IMPLEMENTATION_PLAN.md"},
            }),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        output = json.loads(cp.stdout)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "protected repository path" in output["permissionDecisionReason"]


def test_architect_commit_does_not_require_user_git_identity_or_signing(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td, tempfile.TemporaryDirectory() as home_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        monkeypatch.setenv("HOME", home_td)
        _git(root, "config", "--unset", "user.email", check=False)
        _git(root, "config", "--unset", "user.name", check=False)
        _git(root, "config", "commit.gpgSign", "true")
        hooks = root / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        pre_commit = hooks / "pre-commit"
        pre_commit.write_text("#!/bin/sh\nexit 99\n")
        pre_commit.chmod(0o755)

        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        begin_planning_repair(root, reason="missing verification step")
        monkeypatch.setattr(pr, "provider_from_args", lambda _args: (os.environ.copy(), {"provider": "native"}))

        def fake_control_model(**kwargs):
            worktree = kwargs["root"]
            plan = worktree / "IMPLEMENTATION_PLAN.md"
            plan.write_text(plan.read_text() + "\n2. Verify.\n")
            cp = subprocess.CompletedProcess(["claude"], 0, "", "")
            text = (
                'PLANNING_REPAIR_ARCHITECT: '
                '{"verdict":"READY","classification":"PLAN_PRESERVING","summary":"verified repair"}'
            )
            return cp, text, "architect-session", {}, "SUCCESS", "ok", {}, [], 0.1

        monkeypatch.setattr(pr, "_run_control_model", fake_control_model)
        result = run_planning_repair_architect(
            root,
            SimpleNamespace(reason="repair", model=None, max_budget_usd=None, max_turns=20, timeout=0),
        )
        assert result["status"] == "candidate"
        active = load_active_repair(root)
        worktree = Path(active["worktree"])
        author = _git(worktree, "show", "-s", "--format=%an <%ae>", active["candidate_sha"]).stdout.strip()
        assert author == "Claude Code Autonomous Optimization <claude-auto@localhost.invalid>"


def test_corrupt_durable_planning_policy_blocks_repository_mutation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        state_dir = pr.repo_state_dir(root)
        policy_dir = state_dir / "planning-repair"
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / "policy.json").write_text("{broken")

        stale = make_settings(
            state_dir,
            "external",
            "balanced",
            {"repo_root": str(root), "languages": [], "container_files": []},
        )
        env = os.environ.copy()
        env.update(stale["env"])

        cp = subprocess.run(
            [os.sys.executable, str(WRITE_GUARD)],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "printf 'changed\\n' > app.txt"},
            }),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        output = json.loads(cp.stdout)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "blocked fail-closed" in output["permissionDecisionReason"]


def _p5_member(path: str, *, role: str, repair: str, required: bool = True) -> dict:
    return {
        "path": path,
        "role": role,
        "repair": repair,
        "required": required,
    }


def _p5_helper(helper_id: str, *, inputs: list[str], outputs: list[str]) -> dict:
    return {
        "id": helper_id,
        "argv": ["python3", "-c", "print('ok')"],
        "cwd": ".",
        "inputs": inputs,
        "outputs": outputs,
        "timeout_seconds": 30,
        "capabilities": {"network": [], "read_external": []},
    }


def _p5_set(
    set_id: str,
    members: list[dict],
    *,
    validators: list[dict] | None = None,
    reconcilers: list[dict] | None = None,
) -> dict:
    return {
        "id": set_id,
        "members": members,
        "validators": validators or [],
        "reconcilers": reconcilers or [],
    }


def _p5_contract(sets: list[dict]) -> dict:
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


def _p5_write_governance(root: Path, contract: dict) -> None:
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n")
    _git(root, "add", ".claude-auto/governance.json")
    _git(root, "commit", "-qm", "governance")


def test_p5_legacy_one_file_policy_derives_synthetic_repair_envelope(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")

        envelope = derive_repair_envelope(root, reason="repair ordering")
        assert envelope["source_mode"] == "legacy"
        assert envelope["selected_authority_sets"] == ["default"]
        assert envelope["repairable_paths"] == ["IMPLEMENTATION_PLAN.md"]
        assert envelope["immutable_paths"] == []
        assert envelope["generated_paths"] == []
        assert envelope["allowed_new_repairable_selectors"] == []
        assert envelope["validator_contracts"] == []
        assert envelope["reconciler_contracts"] == []


def test_p5_multi_set_repair_envelope_requires_explicit_selection_and_captures_contract(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "main.md").write_text("plan\n")
        (root / "requirements.md").write_text("immutable\n")
        (root / "generated").mkdir()
        (root / "generated" / "index.json").write_text("{}\n")
        (root / "other.md").write_text("other\n")
        _git(root, "add", "plans", "requirements.md", "generated", "other.md")
        _git(root, "commit", "-qm", "multi authority")

        validator = _p5_helper(
            "validate-a",
            inputs=["plans/*.md", "requirements.md", "generated/*.json"],
            outputs=[],
        )
        reconciler = _p5_helper(
            "generate-a",
            inputs=["plans/*.md", "requirements.md"],
            outputs=["generated/*.json"],
        )
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [
                    _p5_member("plans/*.md", role="source", repair="repairable"),
                    _p5_member("requirements.md", role="contract", repair="immutable"),
                    _p5_member("generated/*.json", role="projection", repair="generated"),
                ],
                validators=[validator],
                reconcilers=[reconciler],
            ),
            _p5_set(
                "b",
                [_p5_member("other.md", role="source", repair="repairable")],
            ),
        ]))

        with pytest.raises(RepairEnvelopeError, match="ambiguous"):
            derive_repair_envelope(root, reason="repair ledger")

        envelope = derive_repair_envelope(
            root,
            reason="repair ledger",
            authority_sets=["a"],
        )
        assert envelope["selected_authority_sets"] == ["a"]
        assert envelope["repairable_paths"] == ["plans/main.md"]
        assert envelope["immutable_paths"] == ["requirements.md"]
        assert envelope["generated_paths"] == ["generated/index.json"]
        assert envelope["allowed_new_repairable_selectors"] == ["plans/*.md"]
        assert envelope["allowed_new_generated_selectors"] == ["generated/*.json"]
        assert [item["helper"]["id"] for item in envelope["validator_contracts"]] == [
            "validate-a"
        ]
        assert [item["helper"]["id"] for item in envelope["reconciler_contracts"]] == [
            "generate-a"
        ]
        assert ".claude-auto/governance.json" in envelope["protected_control_paths"]


def test_p5_repair_envelope_persistence_detects_semantic_tamper(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        envelope = persist_repair_envelope(
            root,
            derive_repair_envelope(root, reason="repair ordering"),
        )
        loaded = load_repair_envelope(root)
        assert loaded is not None
        assert loaded["repair_envelope_sha256"] == envelope["repair_envelope_sha256"]

        path = pr.repo_state_dir(root) / "planning-repair" / "repair-envelope.json"
        raw = json.loads(path.read_text())
        raw["repairable_paths"] = ["app.txt"]
        path.write_text(json.dumps(raw))
        with pytest.raises(RepairEnvelopeError, match="integrity"):
            load_repair_envelope(root)
