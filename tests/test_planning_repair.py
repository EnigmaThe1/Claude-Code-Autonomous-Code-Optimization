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
