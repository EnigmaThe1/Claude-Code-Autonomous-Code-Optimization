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
    run_planning_repair_reconcile,
    validate_planning_repair_candidate,
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
from task_sources import resolve_task_sources


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
        assert "plans/main.md" not in envelope["protected_control_paths"]
        assert "requirements.md" not in envelope["protected_control_paths"]


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


def test_p5_architect_guard_enforces_repair_envelope_mutability_and_growth(monkeypatch):
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
        _p5_write_governance(root, _p5_contract([
            _p5_set("a", [
                _p5_member("plans/*.md", role="source", repair="repairable"),
                _p5_member("requirements.md", role="contract", repair="immutable"),
                _p5_member("generated/*.json", role="projection", repair="generated"),
            ]),
            _p5_set("b", [
                _p5_member("other.md", role="source", repair="repairable"),
            ]),
        ]))
        persist_repair_envelope(
            root,
            derive_repair_envelope(
                root,
                reason="repair planning",
                authority_sets=["a"],
            ),
        )
        env = {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(root),
            "CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE": str(
                pr.repo_state_dir(root) / "planning-repair" / "repair-envelope.json"
            ),
        }

        assert _hook(
            "Edit",
            {"file_path": str(root / "plans" / "main.md")},
            env,
        )["permissionDecision"] == "allow"
        assert _hook(
            "Write",
            {"file_path": str(root / "plans" / "new.md")},
            env,
        )["permissionDecision"] == "allow"

        for denied in (
            root / "requirements.md",
            root / "generated" / "index.json",
            root / "other.md",
            root / ".claude-auto" / "governance.json",
        ):
            assert _hook(
                "Write",
                {"file_path": str(denied)},
                env,
            )["permissionDecision"] == "deny"

        assert _hook(
            "Bash",
            {"command": "rm plans/main.md"},
            env,
        )["permissionDecision"] == "deny"


def test_p5_architect_guard_denies_path_also_owned_by_unselected_authority_set(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "shared.md").write_text("shared\n")
        _git(root, "add", "shared.md")
        _git(root, "commit", "-qm", "shared plan")
        _p5_write_governance(root, _p5_contract([
            _p5_set("a", [
                _p5_member("shared.md", role="source", repair="repairable"),
            ]),
            _p5_set("b", [
                _p5_member("shared.md", role="traceability", repair="repairable"),
            ]),
        ]))
        persist_repair_envelope(
            root,
            derive_repair_envelope(
                root,
                reason="repair set a",
                authority_sets=["a"],
            ),
        )
        env = {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(root),
            "CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE": str(
                pr.repo_state_dir(root) / "planning-repair" / "repair-envelope.json"
            ),
        }
        result = _hook(
            "Edit",
            {"file_path": str(root / "shared.md")},
            env,
        )
        assert result["permissionDecision"] == "deny"
        assert "unselected AuthoritySet" in result["permissionDecisionReason"]


def test_p5_architect_multi_file_candidate_and_package_delete(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "main.md").write_text("main\n")
        (root / "plans" / "obsolete.md").write_text("obsolete\n")
        (root / "requirements.md").write_text("immutable\n")
        _git(root, "add", "plans", "requirements.md")
        _git(root, "commit", "-qm", "multi plan")
        _p5_write_governance(root, _p5_contract([
            _p5_set("a", [
                _p5_member("plans/*.md", role="source", repair="repairable"),
                _p5_member(
                    "requirements.md",
                    role="contract",
                    repair="immutable",
                ),
            ]),
        ]))

        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native"}),
        )

        def fake_control_model(**kwargs):
            worktree = kwargs["root"]
            (worktree / "plans" / "main.md").write_text("main repaired\n")
            (worktree / "plans" / "new.md").write_text("new planning member\n")
            cp = subprocess.CompletedProcess(["claude"], 0, "", "")
            text = (
                'PLANNING_REPAIR_ARCHITECT: '
                '{"verdict":"READY","classification":"PLAN_PRESERVING",'
                '"summary":"repairs selected planning authority",'
                '"delete_paths":["plans/obsolete.md"]}'
            )
            return cp, text, "p5-architect", {}, "SUCCESS", "ok", {}, [], 0.1

        monkeypatch.setattr(pr, "_run_control_model", fake_control_model)
        args = SimpleNamespace(
            reason="repair multi-file authority",
            authority_set=["a"],
            model=None,
            max_budget_usd=None,
            max_turns=20,
            timeout=0,
        )
        result = run_planning_repair_architect(root, args)
        assert result["status"] == "candidate"
        active = load_active_repair(root)
        assert active["schema_version"] == 2
        assert active["status"] == "CANDIDATE"
        assert active["repair_envelope_sha256"]
        assert active["architect_delete_paths"] == ["plans/obsolete.md"]
        changed = pr._changed_paths(
            Path(active["worktree"]),
            active["base_sha"],
            active["candidate_sha"],
        )
        assert changed == {
            "plans/main.md",
            "plans/new.md",
            "plans/obsolete.md",
        }
        assert (
            Path(active["worktree"]) / "requirements.md"
        ).read_text() == "immutable\n"


def test_p5_architect_stops_at_reconciling_when_generated_members_exist(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "main.md").write_text("main\n")
        (root / "generated").mkdir()
        (root / "generated" / "index.json").write_text("{}\n")
        _git(root, "add", "plans", "generated")
        _git(root, "commit", "-qm", "generated plan")
        reconciler = _p5_helper(
            "generate-index",
            inputs=["plans/*.md"],
            outputs=["generated/*.json"],
        )
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [
                    _p5_member(
                        "plans/*.md",
                        role="source",
                        repair="repairable",
                    ),
                    _p5_member(
                        "generated/*.json",
                        role="projection",
                        repair="generated",
                    ),
                ],
                reconcilers=[reconciler],
            ),
        ]))

        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native"}),
        )

        def fake_control_model(**kwargs):
            worktree = kwargs["root"]
            (worktree / "plans" / "main.md").write_text("repaired\n")
            cp = subprocess.CompletedProcess(["claude"], 0, "", "")
            text = (
                'PLANNING_REPAIR_ARCHITECT: '
                '{"verdict":"READY","classification":"PLAN_PRESERVING",'
                '"summary":"repair source before regeneration","delete_paths":[]}'
            )
            return cp, text, "p5-architect", {}, "SUCCESS", "ok", {}, [], 0.1

        monkeypatch.setattr(pr, "_run_control_model", fake_control_model)
        args = SimpleNamespace(
            reason="repair and regenerate",
            authority_set=["a"],
            model=None,
            max_budget_usd=None,
            max_turns=20,
            timeout=0,
        )
        result = run_planning_repair_architect(root, args)
        assert result["status"] == "reconcile-required"
        active = load_active_repair(root)
        assert active["status"] == "RECONCILING"
        assert active["candidate_sha"] is None
        worktree = Path(active["worktree"])
        assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == active[
            "base_sha"
        ]
        assert pr._worktree_dirty_paths(worktree) == {"plans/main.md"}


def _p5_test_runner(
    view: Path,
    command: list[str],
    *,
    timeout: int,
    trust_repo_scripts: bool,
    unrestricted_host: bool,
    read_only_root: bool,
    working_directory: Path,
    hidden_paths: list[Path],
    max_output_bytes: int,
    read_allowlist_only: bool,
) -> dict:
    assert view.resolve() != working_directory.resolve() or working_directory == view
    assert trust_repo_scripts is False
    assert unrestricted_host is False
    assert read_only_root is False
    assert read_allowlist_only is True
    assert hidden_paths
    assert max_output_bytes > 0
    cp = subprocess.run(
        command,
        cwd=working_directory,
        text=True,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "returncode": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
        "timed_out": False,
        "wall_seconds": 0.01,
        "execution_boundary": "test-sandbox",
        "sandboxed": True,
        "environment_scrubbed": True,
    }


def _p5_reconciling_fixture(
    root: Path,
    *,
    helper: dict,
) -> tuple[dict, Path]:
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "main.md").write_text("base plan\n")
    (root / "generated").mkdir(exist_ok=True)
    (root / "generated" / "index.json").write_text('{"state":"base"}\n')
    (root / "generated" / "old.json").write_text('{"old":true}\n')
    _git(root, "add", "plans", "generated")
    _git(root, "commit", "-qm", "planning authority files")
    _p5_write_governance(root, _p5_contract([
        _p5_set(
            "a",
            [
                _p5_member(
                    "plans/*.md",
                    role="source",
                    repair="repairable",
                ),
                _p5_member(
                    "generated/*.json",
                    role="projection",
                    repair="generated",
                ),
            ],
            reconcilers=[helper],
        ),
    ]))
    active = begin_planning_repair(
        root,
        reason="repair and regenerate",
        authority_sets=["a"],
    )
    worktree = Path(active["worktree"])
    (worktree / "plans" / "main.md").write_text("repaired plan\n")
    active = load_active_repair(root)
    active.update({
        "status": "RECONCILING",
        "architect_delete_paths": [],
        "architect_summary": "repair source before regeneration",
        "candidate_sha": None,
        "verified_sha": None,
    })
    json_dump(pr._active_path(root), active)
    return active, worktree


def test_p5_reconciler_deterministically_creates_updates_and_deletes_generated_members(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        code = (
            "from pathlib import Path;"
            "p=Path('plans/main.md').read_text();"
            "Path('generated/index.json').write_text('{\"plan\":'+repr(p)+'}\\n');"
            "Path('generated/new.json').write_text('{\"new\":true}\\n');"
            "Path('generated/old.json').unlink()"
        )
        helper = _p5_helper(
            "generate-index",
            inputs=["plans/*.md"],
            outputs=["generated/*.json"],
        )
        helper["argv"] = ["python3", "-c", code]
        active, worktree = _p5_reconciling_fixture(root, helper=helper)

        # Simulate a half-applied prior reconciliation. The package must restore
        # generated authority to the envelope base before rerunning helpers.
        (worktree / "generated" / "index.json").write_text('{"partial":true}\n')

        seen_initial_generated: list[str] = []

        def runner(*args, **kwargs):
            view = Path(args[0])
            seen_initial_generated.append(
                (view / "generated" / "index.json").read_text()
            )
            return _p5_test_runner(*args, **kwargs)

        result = run_planning_repair_reconcile(root, runner=runner)
        assert result["status"] == "candidate"
        assert len(seen_initial_generated) == 2
        assert seen_initial_generated == [
            '{"state":"base"}\n',
            '{"state":"base"}\n',
        ]
        assert len(result["receipts"]) == 1
        receipt = result["receipts"][0]
        assert receipt["helper_id"] == "generate-index"
        assert receipt["output_state_sha256"]
        assert len(receipt["determinism_runs"]) == 2

        active = load_active_repair(root)
        assert active["status"] == "CANDIDATE"
        assert active["candidate_sha"] == result["candidate_sha"]
        assert active["reconciler_receipt_bundle_sha256"] == result[
            "reconciler_receipt_bundle_sha256"
        ]
        changed = pr._changed_paths(
            worktree,
            active["base_sha"],
            active["candidate_sha"],
        )
        assert changed == {
            "plans/main.md",
            "generated/index.json",
            "generated/new.json",
            "generated/old.json",
        }
        assert (worktree / "generated" / "new.json").read_text() == '{"new":true}\n'
        assert not (worktree / "generated" / "old.json").exists()
        assert "repaired plan" in (worktree / "generated" / "index.json").read_text()


@pytest.mark.parametrize("capability", ["network", "read_external"])
def test_p5_reconciler_refuses_network_or_external_read_capability(monkeypatch, capability):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        helper = _p5_helper(
            "generate-index",
            inputs=["plans/*.md"],
            outputs=["generated/*.json"],
        )
        helper["capabilities"][capability] = ["forbidden"]
        _active, _worktree = _p5_reconciling_fixture(root, helper=helper)

        called = False

        def should_not_run(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("capability-denied reconciler must not execute")

        with pytest.raises(ValueError, match="does not grant"):
            run_planning_repair_reconcile(root, runner=should_not_run)
        assert called is False
        assert load_active_repair(root)["status"] == "RECONCILING"


def test_p5_reconciler_rejects_undeclared_output_mutation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        helper = _p5_helper(
            "generate-index",
            inputs=["plans/*.md"],
            outputs=["generated/*.json"],
        )
        helper["argv"] = [
            "python3",
            "-c",
            "from pathlib import Path;"
            "Path('generated/index.json').write_text('{}\\n');"
            "Path('escape.txt').write_text('bad\\n')",
        ]
        _active, worktree = _p5_reconciling_fixture(root, helper=helper)
        before_escape = worktree / "escape.txt"

        with pytest.raises(ValueError, match="undeclared output path"):
            run_planning_repair_reconcile(root, runner=_p5_test_runner)
        assert not before_escape.exists()
        assert load_active_repair(root)["status"] == "RECONCILING"


def test_p5_reconciler_rejects_output_declared_as_repairable_not_generated(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        helper = _p5_helper(
            "bad-generator",
            inputs=["plans/*.md"],
            outputs=["plans/*.md"],
        )
        _active, _worktree = _p5_reconciling_fixture(root, helper=helper)

        with pytest.raises(ValueError, match="non-generated output"):
            run_planning_repair_reconcile(root, runner=_p5_test_runner)


def test_p5_reconciler_rejects_nondeterministic_output_bytes(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        helper = _p5_helper(
            "generate-index",
            inputs=["plans/*.md"],
            outputs=["generated/*.json"],
        )
        _active, worktree = _p5_reconciling_fixture(root, helper=helper)
        calls = 0

        def nondeterministic_runner(
            view: Path,
            command: list[str],
            **kwargs,
        ) -> dict:
            nonlocal calls
            calls += 1
            (view / "generated" / "index.json").write_text(
                f'{{"run":{calls}}}\\n'
            )
            return {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "wall_seconds": 0.01,
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(ValueError, match="nondeterministic"):
            run_planning_repair_reconcile(
                root,
                runner=nondeterministic_runner,
            )
        assert calls == 2
        # No nondeterministic temporary output may be copied into the live repair.
        assert (worktree / "generated" / "index.json").read_text() == '{"state":"base"}\n'


def test_p5_planning_reconcile_cli_surface_exists():
    from cli_schema import build_parser

    args = build_parser("test").parse_args(["planning-repair", "reconcile"])
    assert args.planning_repair_command == "reconcile"


def _p5_task(task_id: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": ["a"],
        "depends_on": depends_on or [],
        "owned_paths": [f"src/{task_id}/**"],
        "evidence_paths": [f"evidence/{task_id}/**"],
        "runtime_scratch_paths": [f".cache/{task_id}/**"],
        "verification": ["test"],
        "commit_subject": None,
        "metadata": {},
    }


def _p5_candidate_commit(
    root: Path,
    worktree: Path,
    *,
    active: dict,
    paths: list[str],
    message: str = "planning candidate",
    delete_paths: list[str] | None = None,
) -> str:
    _git(worktree, "add", "-A", "--", *paths)
    _git(worktree, "commit", "-qm", message)
    candidate = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    active = load_active_repair(root)
    active.update({
        "status": "CANDIDATE",
        "candidate_sha": candidate,
        "verified_sha": None,
        "architect_delete_paths": delete_paths or [],
        "candidate_created_at": pr.utcnow(),
    })
    json_dump(pr._active_path(root), active)
    return candidate


def test_p5_candidate_task_sources_validate_without_overwriting_live_runtime_state(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        ledger = root / "plans" / "tasks.json"
        ledger.write_text(json.dumps({
            "schema_version": 1,
            "tasks": [_p5_task("T1")],
        }) + "\n")
        _git(root, "add", "plans/tasks.json")
        _git(root, "commit", "-qm", "task ledger")

        contract = _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/tasks.json",
                    role="source",
                    repair="repairable",
                )],
            ),
        ])
        contract["tasks"]["sources"] = [{
            "id": "ledger",
            "kind": "json",
            "authority_sets": ["a"],
            "paths": ["plans/tasks.json"],
        }]
        _p5_write_governance(root, contract)

        live = resolve_task_sources(root, persist=True)
        assert live["status"] == "READY"
        persisted = pr.repo_state_dir(root) / "tasks" / "task-source-set.json"
        persisted_before = persisted.read_bytes()
        state_before = json.loads(
            (pr.repo_state_dir(root) / "state.json").read_text()
        )

        active = begin_planning_repair(
            root,
            reason="add dependent task",
            authority_sets=["a"],
        )
        worktree = Path(active["worktree"])
        (worktree / "plans" / "tasks.json").write_text(json.dumps({
            "schema_version": 1,
            "tasks": [
                _p5_task("T1"),
                _p5_task("T2", depends_on=["T1"]),
            ],
        }) + "\n")
        candidate = _p5_candidate_commit(
            root,
            worktree,
            active=active,
            paths=["plans/tasks.json"],
        )

        result = validate_planning_repair_candidate(root)
        assert result["status"] == "valid"
        assert result["candidate_sha"] == candidate
        assert result["candidate_task_source_set_sha256"]
        assert result["candidate_task_source_set_sha256"] != live[
            "task_source_set_sha256"
        ]
        assert persisted.read_bytes() == persisted_before
        state_after = json.loads(
            (pr.repo_state_dir(root) / "state.json").read_text()
        )
        assert state_after["task_source_sha256"] == state_before[
            "task_source_sha256"
        ]

        active = load_active_repair(root)
        assert active["validated_candidate_sha"] == candidate
        assert active["candidate_authority_evidence_sha256"]
        evidence = json.loads(
            Path(active["candidate_authority_evidence_path"]).read_text()
        )
        assert evidence["candidate_task_sources"]["status"] == "READY"
        assert evidence["candidate_task_sources"][
            "task_source_set_sha256"
        ] == result["candidate_task_source_set_sha256"]


def test_p5_candidate_task_source_cycle_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "tasks.json").write_text(json.dumps({
            "schema_version": 1,
            "tasks": [_p5_task("T1")],
        }) + "\n")
        _git(root, "add", "plans/tasks.json")
        _git(root, "commit", "-qm", "task ledger")
        contract = _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/tasks.json",
                    role="source",
                    repair="repairable",
                )],
            ),
        ])
        contract["tasks"]["sources"] = [{
            "id": "ledger",
            "kind": "json",
            "authority_sets": ["a"],
            "paths": ["plans/tasks.json"],
        }]
        _p5_write_governance(root, contract)

        active = begin_planning_repair(
            root,
            reason="bad cyclic repair",
            authority_sets=["a"],
        )
        worktree = Path(active["worktree"])
        (worktree / "plans" / "tasks.json").write_text(json.dumps({
            "schema_version": 1,
            "tasks": [
                _p5_task("T1", depends_on=["T2"]),
                _p5_task("T2", depends_on=["T1"]),
            ],
        }) + "\n")
        _p5_candidate_commit(
            root,
            worktree,
            active=active,
            paths=["plans/tasks.json"],
        )
        with pytest.raises(ValueError, match="TaskSource validation failed|cycle"):
            validate_planning_repair_candidate(root)


def test_p5_candidate_required_authority_member_deletion_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "main.md").write_text("required\n")
        _git(root, "add", "plans/main.md")
        _git(root, "commit", "-qm", "required planning member")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/main.md",
                    role="source",
                    repair="repairable",
                    required=True,
                )],
            ),
        ]))

        active = begin_planning_repair(
            root,
            reason="invalid deletion",
            authority_sets=["a"],
        )
        worktree = Path(active["worktree"])
        (worktree / "plans" / "main.md").unlink()
        _p5_candidate_commit(
            root,
            worktree,
            active=active,
            paths=["plans/main.md"],
            delete_paths=["plans/main.md"],
        )
        with pytest.raises(ValueError, match="required authority selector"):
            validate_planning_repair_candidate(root)


def test_p5_candidate_authority_case_collision_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "base.md").write_text("base\n")
        _git(root, "add", "plans/base.md")
        _git(root, "commit", "-qm", "pattern authority")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/*.md",
                    role="source",
                    repair="repairable",
                )],
            ),
        ]))

        active = begin_planning_repair(
            root,
            reason="collision",
            authority_sets=["a"],
        )
        worktree = Path(active["worktree"])
        (worktree / "plans" / "Case.md").write_text("one\n")
        (worktree / "plans" / "case.md").write_text("two\n")
        _p5_candidate_commit(
            root,
            worktree,
            active=active,
            paths=["plans/Case.md", "plans/case.md"],
        )
        with pytest.raises(ValueError, match="case/Unicode-colliding"):
            validate_planning_repair_candidate(root)


def test_p5_planning_validate_cli_surface_exists():
    from cli_schema import build_parser

    args = build_parser("test").parse_args(["planning-repair", "validate"])
    assert args.planning_repair_command == "validate"


def _p5_validator_candidate_fixture(
    root: Path,
    *,
    validator: dict,
) -> tuple[dict, Path, str]:
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "main.md").write_text("base\n")
    _git(root, "add", "plans/main.md")
    _git(root, "commit", "-qm", "validator planning input")
    _p5_write_governance(root, _p5_contract([
        _p5_set(
            "a",
            [_p5_member(
                "plans/main.md",
                role="source",
                repair="repairable",
            )],
            validators=[validator],
        ),
    ]))
    active = begin_planning_repair(
        root,
        reason="validate repaired planning",
        authority_sets=["a"],
    )
    worktree = Path(active["worktree"])
    (worktree / "plans" / "main.md").write_text("repaired\n")
    candidate = _p5_candidate_commit(
        root,
        worktree,
        active=active,
        paths=["plans/main.md"],
    )
    return load_active_repair(root), worktree, candidate


def test_p5_validator_runs_twice_against_exact_candidate_inputs(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        validator = _p5_helper(
            "validate-plan",
            inputs=["plans/main.md"],
            outputs=[],
        )
        active, _worktree, candidate = _p5_validator_candidate_fixture(
            root,
            validator=validator,
        )
        seen: list[str] = []

        def runner(*args, **kwargs):
            view = Path(args[0])
            seen.append((view / "plans" / "main.md").read_text())
            return _p5_test_runner(*args, **kwargs)

        result = validate_planning_repair_candidate(
            root,
            validator_runner=runner,
        )
        assert result["status"] == "valid"
        assert seen == ["repaired\n", "repaired\n"]
        assert result["validator_receipt_bundle_sha256"]
        active = load_active_repair(root)
        assert active["validated_candidate_sha"] == candidate
        assert active["validator_receipt_bundle_sha256"] == result[
            "validator_receipt_bundle_sha256"
        ]
        bundle = json.loads(Path(active["validator_receipt_path"]).read_text())
        assert bundle["candidate_sha"] == candidate
        assert len(bundle["receipts"]) == 1
        assert len(bundle["receipts"][0]["determinism_runs"]) == 2


def test_p5_validator_nonzero_exit_rejects_candidate(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        validator = _p5_helper(
            "validate-plan",
            inputs=["plans/main.md"],
            outputs=[],
        )
        validator["argv"] = ["python3", "-c", "raise SystemExit(3)"]
        _p5_validator_candidate_fixture(root, validator=validator)

        with pytest.raises(ValueError, match="failed with exit 3"):
            validate_planning_repair_candidate(
                root,
                validator_runner=_p5_test_runner,
            )
        assert load_active_repair(root)["validated_candidate_sha"] is None if "validated_candidate_sha" in load_active_repair(root) else True


def test_p5_validator_timeout_or_unavailable_boundary_rejects(monkeypatch):
    for mode in ("timeout", "unavailable"):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = _repo(Path(td) / "repo")
            monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
            validator = _p5_helper(
                "validate-plan",
                inputs=["plans/main.md"],
                outputs=[],
            )
            _p5_validator_candidate_fixture(root, validator=validator)

            def runner(*args, **kwargs):
                if mode == "timeout":
                    return {
                        "returncode": 124,
                        "stdout": "",
                        "stderr": "timed out",
                        "timed_out": True,
                        "wall_seconds": 30.0,
                        "execution_boundary": "test-sandbox",
                        "sandboxed": True,
                        "environment_scrubbed": True,
                    }
                return {
                    "returncode": 125,
                    "stdout": "",
                    "stderr": "no sandbox",
                    "timed_out": False,
                    "wall_seconds": 0.0,
                    "execution_boundary": "unavailable",
                    "sandboxed": False,
                    "environment_scrbed": True,
                    "environment_scrubbed": True,
                }

            with pytest.raises(ValueError, match="timed out|verified isolation boundary"):
                validate_planning_repair_candidate(
                    root,
                    validator_runner=runner,
                )


def test_p5_validator_cannot_mutate_exact_candidate_input(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        validator = _p5_helper(
            "validate-plan",
            inputs=["plans/main.md"],
            outputs=[],
        )
        validator["argv"] = [
            "python3",
            "-c",
            "from pathlib import Path;Path('plans/main.md').write_text('tampered\\n')",
        ]
        _p5_validator_candidate_fixture(root, validator=validator)

        with pytest.raises(ValueError, match="mutated exact candidate input"):
            validate_planning_repair_candidate(
                root,
                validator_runner=_p5_test_runner,
            )


def test_p5_validator_nondeterministic_pass_fail_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        validator = _p5_helper(
            "validate-plan",
            inputs=["plans/main.md"],
            outputs=[],
        )
        _p5_validator_candidate_fixture(root, validator=validator)
        calls = 0

        def runner(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {
                "returncode": 0 if calls == 1 else 1,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "wall_seconds": 0.01,
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(ValueError, match="nondeterministic pass/fail"):
            validate_planning_repair_candidate(
                root,
                validator_runner=runner,
            )
        assert calls == 2


def _p5_verified_candidate_fixture(root: Path) -> tuple[dict, Path, str]:
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "main.md").write_text("base\n")
    _git(root, "add", "plans/main.md")
    _git(root, "commit", "-qm", "verifier planning input")
    _p5_write_governance(root, _p5_contract([
        _p5_set(
            "a",
            [_p5_member(
                "plans/main.md",
                role="source",
                repair="repairable",
            )],
        ),
    ]))
    active = begin_planning_repair(
        root,
        reason="independent verifier repair",
        authority_sets=["a"],
    )
    worktree = Path(active["worktree"])
    (worktree / "plans" / "main.md").write_text("repaired\n")
    candidate = _p5_candidate_commit(
        root,
        worktree,
        active=active,
        paths=["plans/main.md"],
    )
    validate_planning_repair_candidate(root)
    return load_active_repair(root), worktree, candidate


def _p5_verifier_args(**overrides):
    data = {
        "sha": None,
        "model": "verifier-model",
        "timeout": 60,
        "max_turns": 20,
        "max_budget_usd": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_p5_independent_verifier_binds_exact_sha_envelope_and_enriched_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        active, _worktree, candidate = _p5_verified_candidate_fixture(root)
        envelope = load_repair_envelope(root)
        assert envelope is not None
        envelope_sha = envelope["repair_envelope_sha256"]

        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native-test"}),
        )
        seen_prompt: list[str] = []

        def fake_verifier(**kwargs):
            seen_prompt.append(kwargs["prompt"])
            return (
                "PLANNING_REPAIR_VERIFY: "
                + json.dumps({
                    "verdict": "VERIFIED",
                    "candidate_sha": candidate,
                    "repair_envelope_sha256": envelope_sha,
                    "summary": "exact candidate preserves planning intent",
                    "findings": [],
                }),
                {
                    "usage": {},
                    "repository_unchanged": True,
                    "git_after": {"head": candidate},
                },
            )

        monkeypatch.setattr(pr, "run_readonly_plan_agent", fake_verifier)
        result = verify_planning_repair(root, _p5_verifier_args())
        assert result["status"] == "verified"
        assert result["candidate_sha"] == candidate
        assert result["repair_envelope_sha256"] == envelope_sha
        assert candidate in seen_prompt[0]
        assert envelope_sha in seen_prompt[0]

        attestation = load_promotion_attestation(
            root,
            candidate,
            PLANNING_REPAIR_CONTRACT,
        )
        assert attestation["target_sha"] == candidate
        metadata = attestation["metadata"]
        assert metadata["repair_envelope_sha256"] == envelope_sha
        assert metadata["selected_authority_sets"] == ["a"]
        assert metadata["base_authority_content_sha256"] == envelope[
            "base_authority_content_sha256"
        ]
        assert metadata["candidate_authority_content_sha256"] == active[
            "candidate_authority_content_sha256"
        ]
        assert metadata["candidate_authority_evidence_sha256"] == active[
            "candidate_authority_evidence_sha256"
        ]
        assert metadata["validator_receipt_bundle_sha256"] == active[
            "validator_receipt_bundle_sha256"
        ]
        assert metadata["reconciler_receipt_bundle_sha256"] is None
        assert metadata["repository_unchanged"] is True

        current = load_active_repair(root)
        assert current["verified_sha"] == candidate
        assert current["last_verifier"]["verdict"] == "VERIFIED"
        assert current["last_verifier"]["candidate_sha"] == candidate
        assert current["last_verifier"]["repair_envelope_sha256"] == envelope_sha


@pytest.mark.parametrize("mismatch", ["sha", "envelope"])
def test_p5_independent_verifier_rejects_protocol_identity_mismatch(monkeypatch, mismatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        _active, _worktree, candidate = _p5_verified_candidate_fixture(root)
        envelope = load_repair_envelope(root)
        assert envelope is not None
        envelope_sha = envelope["repair_envelope_sha256"]

        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native-test"}),
        )

        def fake_verifier(**kwargs):
            payload = {
                "verdict": "VERIFIED",
                "candidate_sha": (
                    "0" * len(candidate) if mismatch == "sha" else candidate
                ),
                "repair_envelope_sha256": (
                    "f" * 64 if mismatch == "envelope" else envelope_sha
                ),
                "summary": "mismatched identity",
                "findings": [],
            }
            return (
                "PLANNING_REPAIR_VERIFY: " + json.dumps(payload),
                {
                    "usage": {},
                    "repository_unchanged": True,
                    "git_after": {"head": candidate},
                },
            )

        monkeypatch.setattr(pr, "run_readonly_plan_agent", fake_verifier)
        with pytest.raises(ValueError, match="wrong candidate SHA|wrong RepairEnvelope"):
            verify_planning_repair(root, _p5_verifier_args())
        assert not load_promotion_attestation(
            root,
            candidate,
            PLANNING_REPAIR_CONTRACT,
        )


@pytest.mark.parametrize("evidence_kind", ["authority", "validator"])
def test_p5_independent_verifier_fails_before_model_on_tampered_evidence(monkeypatch, evidence_kind):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        active, _worktree, candidate = _p5_verified_candidate_fixture(root)

        path_key = (
            "candidate_authority_evidence_path"
            if evidence_kind == "authority"
            else "validator_receipt_path"
        )
        path = Path(active[path_key])
        raw = json.loads(path.read_text())
        if evidence_kind == "authority":
            raw["candidate_authority_content_sha256"] = "0" * 64
        else:
            raw["receipts"] = [{"tampered": True}]
        path.write_text(json.dumps(raw))

        called = False

        def should_not_run(**kwargs):
            nonlocal called
            called = True
            raise AssertionError("verifier must not run on tampered package evidence")

        monkeypatch.setattr(pr, "run_readonly_plan_agent", should_not_run)
        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native-test"}),
        )
        with pytest.raises(ValueError, match="integrity"):
            verify_planning_repair(root, _p5_verifier_args())
        assert called is False
        assert not load_promotion_attestation(
            root,
            candidate,
            PLANNING_REPAIR_CONTRACT,
        )


@pytest.mark.parametrize("verdict", ["REJECTED", "BLOCKED"])
def test_p5_independent_verifier_nonverified_outcome_creates_no_attestation(monkeypatch, verdict):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        _active, _worktree, candidate = _p5_verified_candidate_fixture(root)
        envelope = load_repair_envelope(root)
        assert envelope is not None
        envelope_sha = envelope["repair_envelope_sha256"]

        monkeypatch.setattr(
            pr,
            "provider_from_args",
            lambda _args: (os.environ.copy(), {"provider": "native-test"}),
        )
        monkeypatch.setattr(
            pr,
            "run_readonly_plan_agent",
            lambda **_kwargs: (
                "PLANNING_REPAIR_VERIFY: "
                + json.dumps({
                    "verdict": verdict,
                    "candidate_sha": candidate,
                    "repair_envelope_sha256": envelope_sha,
                    "summary": "not acceptable",
                    "findings": ["finding"],
                }),
                {
                    "usage": {},
                    "repository_unchanged": True,
                    "git_after": {"head": candidate},
                },
            ),
        )
        result = verify_planning_repair(root, _p5_verifier_args())
        assert result["status"] == verdict.lower()
        assert result["findings"] == ["finding"]
        assert not load_promotion_attestation(
            root,
            candidate,
            PLANNING_REPAIR_CONTRACT,
        )
        current = load_active_repair(root)
        assert current["verified_sha"] is None
        assert current["last_verifier"]["verdict"] == verdict


def test_p5_large_multifile_authority_envelope_is_deterministic(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        plans = root / "plans"
        plans.mkdir()
        expected = []
        for index in range(300):
            rel = f"plans/plan-{index:03d}.md"
            (root / rel).write_text(f"plan {index}\n")
            expected.append(rel)
        _git(root, "add", "plans")
        _git(root, "commit", "-qm", "large planning authority")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/*.md",
                    role="source",
                    repair="repairable",
                )],
            ),
        ]))

        first = derive_repair_envelope(
            root,
            reason="large multi-file repair",
            authority_sets=["a"],
        )
        second = derive_repair_envelope(
            root,
            reason="large multi-file repair",
            authority_sets=["a"],
        )
        assert first["repairable_paths"] == expected
        assert second["repairable_paths"] == expected
        assert first["repair_envelope_sha256"] == second[
            "repair_envelope_sha256"
        ]


def test_p5_unicode_space_repairable_path_is_preserved_exactly(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        plans = root / "plans"
        plans.mkdir()
        rel = "plans/Δ planning file.md"
        (root / rel).write_text("unicode plan\n")
        _git(root, "add", "--", rel)
        _git(root, "commit", "-qm", "unicode planning member")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    rel,
                    role="source",
                    repair="repairable",
                )],
            ),
        ]))

        active = begin_planning_repair(
            root,
            reason="repair unicode planning member",
            authority_sets=["a"],
        )
        envelope = load_repair_envelope(root)
        assert envelope is not None
        assert envelope["repairable_paths"] == [rel]

        env = {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": active["worktree"],
            "CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE": str(
                pr.repo_state_dir(root)
                / "planning-repair"
                / "repair-envelope.json"
            ),
        }
        result = _hook(
            "Write",
            {"file_path": str(Path(active["worktree"]) / rel)},
            env,
        )
        assert result["permissionDecision"] == "allow"


def test_p5_symlink_authority_member_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        plans = root / "plans"
        plans.mkdir()
        (plans / "real.md").write_text("real\n")
        (plans / "link.md").symlink_to("real.md")
        _git(root, "add", "plans")
        _git(root, "commit", "-qm", "symlink planning member")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "plans/link.md",
                    role="source",
                    repair="repairable",
                )],
            ),
        ]))

        with pytest.raises(RepairEnvelopeError, match="symlink"):
            derive_repair_envelope(
                root,
                reason="unsafe symlink authority",
                authority_sets=["a"],
            )


def test_p5_gitlink_authority_member_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        object_id = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{object_id},nested",
        )
        _git(root, "commit", "-qm", "gitlink planning member")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [_p5_member(
                    "nested",
                    role="source",
                    repair="repairable",
                )],
            ),
        ]))

        with pytest.raises(RepairEnvelopeError, match="gitlink|submodule"):
            derive_repair_envelope(
                root,
                reason="unsafe gitlink authority",
                authority_sets=["a"],
            )


def test_p5_unattended_does_not_bypass_repair_envelope(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "plans").mkdir()
        (root / "plans" / "main.md").write_text("repairable\n")
        (root / "requirements.md").write_text("immutable\n")
        _git(root, "add", "plans/main.md", "requirements.md")
        _git(root, "commit", "-qm", "planning authority")
        _p5_write_governance(root, _p5_contract([
            _p5_set(
                "a",
                [
                    _p5_member(
                        "plans/main.md",
                        role="source",
                        repair="repairable",
                    ),
                    _p5_member(
                        "requirements.md",
                        role="contract",
                        repair="immutable",
                    ),
                ],
            ),
        ]))
        active = begin_planning_repair(
            root,
            reason="unattended governance qualification",
            authority_sets=["a"],
        )
        worktree = Path(active["worktree"])
        env = {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(worktree),
            "CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE": str(
                pr.repo_state_dir(root)
                / "planning-repair"
                / "repair-envelope.json"
            ),
            "CLAUDE_AUTONOMY_PROFILE": "unattended",
            "CLAUDE_AUTO_SEMANTIC_ONLY_WRITE_GUARD": "1",
        }

        allowed = _hook(
            "Write",
            {"file_path": str(worktree / "plans" / "main.md")},
            env,
        )
        denied = _hook(
            "Write",
            {"file_path": str(worktree / "requirements.md")},
            env,
        )
        bash = _hook(
            "Bash",
            {"command": "printf x > plans/main.md"},
            env,
        )
        assert allowed["permissionDecision"] == "allow"
        assert denied["permissionDecision"] == "deny"
        assert "immutable" in denied["permissionDecisionReason"].lower()
        assert bash["permissionDecision"] == "deny"
