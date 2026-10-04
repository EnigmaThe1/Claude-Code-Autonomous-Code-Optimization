from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace


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
from settings_policy import make_settings
from state_store import json_dump


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks" / "planning_repair_guard.py"


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
