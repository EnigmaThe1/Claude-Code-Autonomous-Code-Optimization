from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import claude_auto as ca
import planning_repair as pr
from planning_repair import configure_planning_repair
from state_store import json_dump


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return cp.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n\n1. Original.\n")
    (path / "app.txt").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        max_plan_revisions=3,
        cycle_timeout=0,
        max_turns=30,
        max_budget_usd=None,
        verifier_model=None,
        model=None,
        profile="balanced",
        _permission_grants=[],
        _permission_overrides=[],
    )


def test_repository_plan_policy_requires_exact_supplied_canonical_plan(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")

        assert "must use the canonical plan" in ca._repository_plan_policy_error(
            root, source_kind="generated", source_ref=None
        )
        assert "divergent planning authorities" in ca._repository_plan_policy_error(
            root, source_kind="supplied-plan", source_ref="OTHER_PLAN.md"
        )
        assert ca._repository_plan_policy_error(
            root, source_kind="supplied-plan", source_ref="IMPLEMENTATION_PLAN.md"
        ) is None


def test_supervisor_repair_chain_promotes_then_rereads_canonical_plan(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        sd = ca.repo_state_dir(root)
        state = {"objective": "Build the requested product.", "autonomy_profile": "balanced"}
        json_dump(sd / "state.json", state)

        active = {
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "candidate_sha": None,
            "verified_sha": None,
        }
        calls: list[str] = []

        def fake_begin(_root, reason=""):
            calls.append("begin")
            return dict(active)

        def fake_load(_root):
            return dict(active)

        def fake_refresh(_root):
            calls.append("refresh")
            return {"status": "unchanged"}

        def fake_architect(_root, _args):
            calls.append("architect")
            active["candidate_sha"] = "a" * 40
            return {
                "status": "candidate",
                "candidate_sha": active["candidate_sha"],
                "classification": "PLAN_PRESERVING",
                "summary": "repair prepared",
            }

        def fake_verify(_root, _args):
            calls.append("verify")
            active["verified_sha"] = active["candidate_sha"]
            return {
                "status": "verified",
                "candidate_sha": active["candidate_sha"],
                "summary": "exact SHA verified",
            }

        def fake_promote(_root):
            calls.append("promote")
            (root / "IMPLEMENTATION_PLAN.md").write_text(
                "# Plan\n\n1. Original.\n2. Promoted verified repair.\n"
            )
            return {"status": "promoted", "head": active["candidate_sha"]}

        captured: dict[str, object] = {}

        def fake_validate(**kwargs):
            calls.append("validate")
            captured["source_text"] = kwargs["source_text"]
            captured["source_hash"] = kwargs["source_hash"]
            plan = {"objective": kwargs["objective"], "version": 2, "tasks": [{"id": "T1"}]}
            st = json.loads((sd / "state.json").read_text())
            st["plan_status"] = "VALIDATED"
            json_dump(sd / "state.json", st)
            return plan, 0

        monkeypatch.setattr(ca, "begin_planning_repair", fake_begin)
        monkeypatch.setattr(ca, "load_active_repair", fake_load)
        monkeypatch.setattr(ca, "refresh_planning_repair_base", fake_refresh)
        monkeypatch.setattr(ca, "run_planning_repair_architect", fake_architect)
        monkeypatch.setattr(ca, "verify_planning_repair", fake_verify)
        monkeypatch.setattr(ca, "promote_planning_repair", fake_promote)
        monkeypatch.setattr(ca, "ensure_plan_validated", fake_validate)
        monkeypatch.setattr(ca, "ensure_verification_baseline", lambda *_a, **_k: [])
        monkeypatch.setattr(ca, "_baseline_integrity_error", lambda _r: None)
        monkeypatch.setattr(ca, "reset_permission_epoch", lambda *_a, **_k: False)

        initial = (root / "IMPLEMENTATION_PLAN.md").read_text()
        source_hash = ca._source_identity_hash(
            "supplied-plan", "IMPLEMENTATION_PLAN.md", state["objective"], initial
        )
        plan, rc, new_text, new_hash, _state = ca._revalidate_authoritative_plan(
            args=_args(),
            root=root,
            sd=sd,
            prof={},
            state=state,
            objective=state["objective"],
            source_kind="supplied-plan",
            source_ref="IMPLEMENTATION_PLAN.md",
            source_text=initial,
            source_hash=source_hash,
            env=os.environ.copy(),
            provider_detail={"provider": "native"},
            reason="material plan defect",
        )

        assert rc == 0
        assert plan and plan["version"] == 2
        assert "Promoted verified repair" in new_text
        assert new_hash != source_hash
        assert captured["source_text"] == new_text
        assert captured["source_hash"] == new_hash
        assert calls == [
            "begin", "refresh", "architect", "verify", "refresh",
            "promote", "validate",
        ]


def test_supervisor_resumes_already_verified_candidate_without_reauthoring(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        sd = ca.repo_state_dir(root)
        candidate = "b" * 40
        active = {
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "candidate_sha": candidate,
            "verified_sha": candidate,
        }
        state = {"objective": "Build the requested product.", "autonomy_profile": "balanced"}
        json_dump(sd / "state.json", state)
        calls: list[str] = []

        monkeypatch.setattr(ca, "load_active_repair", lambda _r: dict(active))
        monkeypatch.setattr(
            ca, "begin_planning_repair",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not begin")),
        )
        monkeypatch.setattr(
            ca, "run_planning_repair_architect",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not architect")),
        )
        monkeypatch.setattr(
            ca, "verify_planning_repair",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not verify")),
        )
        monkeypatch.setattr(ca, "refresh_planning_repair_base", lambda _r: calls.append("refresh") or {"status": "unchanged"})

        def promote(_root):
            calls.append("promote")
            (root / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n\n1. Verified resume.\n")
            return {"status": "promoted", "head": candidate}

        monkeypatch.setattr(ca, "promote_planning_repair", promote)
        monkeypatch.setattr(
            ca, "ensure_plan_validated",
            lambda **kwargs: ({"version": 2, "objective": kwargs["objective"]}, 0),
        )
        monkeypatch.setattr(ca, "ensure_verification_baseline", lambda *_a, **_k: [])
        monkeypatch.setattr(ca, "_baseline_integrity_error", lambda _r: None)
        monkeypatch.setattr(ca, "reset_permission_epoch", lambda *_a, **_k: False)

        initial = (root / "IMPLEMENTATION_PLAN.md").read_text()
        source_hash = ca._source_identity_hash(
            "supplied-plan", "IMPLEMENTATION_PLAN.md", state["objective"], initial
        )
        _plan, rc, new_text, _new_hash, _state = ca._revalidate_authoritative_plan(
            args=_args(), root=root, sd=sd, prof={}, state=state,
            objective=state["objective"], source_kind="supplied-plan",
            source_ref="IMPLEMENTATION_PLAN.md", source_text=initial,
            source_hash=source_hash, env=os.environ.copy(),
            provider_detail={"provider": "native"}, reason="resume material repair",
        )
        assert rc == 0
        assert "Verified resume" in new_text
        assert calls == ["refresh", "refresh", "promote"]


def test_semantic_decision_blocks_instead_of_rewriting_external_plan(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        root = _repo(Path(td) / "repo")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")
        sd = ca.repo_state_dir(root)
        state = {"objective": "Build the requested product.", "autonomy_profile": "balanced"}
        json_dump(sd / "state.json", state)
        active = {"base_sha": _git(root, "rev-parse", "HEAD"), "candidate_sha": None, "verified_sha": None}

        monkeypatch.setattr(ca, "begin_planning_repair", lambda _r, reason="": dict(active))
        monkeypatch.setattr(ca, "load_active_repair", lambda _r: dict(active))
        monkeypatch.setattr(ca, "refresh_planning_repair_base", lambda _r: {"status": "unchanged"})
        monkeypatch.setattr(
            ca,
            "run_planning_repair_architect",
            lambda *_a, **_k: {
                "status": "blocked",
                "classification": "SEMANTIC_DECISION",
                "summary": "two incompatible product requirements require the operator",
            },
        )
        monkeypatch.setattr(
            ca, "ensure_plan_validated",
            lambda **_k: (_ for _ in ()).throw(AssertionError("external planner must not override semantic decision")),
        )

        initial = (root / "IMPLEMENTATION_PLAN.md").read_text()
        source_hash = ca._source_identity_hash(
            "supplied-plan", "IMPLEMENTATION_PLAN.md", state["objective"], initial
        )
        plan, rc, _text, _hash, final_state = ca._revalidate_authoritative_plan(
            args=_args(), root=root, sd=sd, prof={}, state=state,
            objective=state["objective"], source_kind="supplied-plan",
            source_ref="IMPLEMENTATION_PLAN.md", source_text=initial,
            source_hash=source_hash, env=os.environ.copy(),
            provider_detail={"provider": "native"}, reason="material ambiguity",
        )
        assert plan is None
        assert rc == 3
        assert final_state["status"] == "BLOCKED"
        assert "semantic/user decision" in final_state["blocker"]
