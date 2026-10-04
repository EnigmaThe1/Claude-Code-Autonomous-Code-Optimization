from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane import _completion_source_unchanged
from planning_support import validate_plan_graph
from repo_profile import load_declared_verification_commands
from repo_runtime import git_snapshot
from service_manager import _service_unit_text
from supervisor_support import progress_fingerprint
from telemetry import (
    circuit_breaker_reason,
    effective_invocation_budget,
    update_global_usage,
    usage_from_result,
)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "baseline"], check=True)
    return path


def _task(i: int, deps: list[str]) -> dict:
    return {
        "id": f"T{i:05d}",
        "title": f"Task {i}",
        "depends_on": deps,
        "verification": ["check"],
        "risk": "low",
    }


def test_deep_5000_task_cycle_is_rejected_without_recursion_error():
    tasks = [_task(i, [] if i == 0 else [f"T{i-1:05d}"]) for i in range(5000)]
    tasks[0]["depends_on"] = ["T04999"]
    errors = validate_plan_graph({"acceptance_criteria": ["cycle rejected"], "tasks": tasks})
    assert any("dependency cycle:" in x for x in errors)


def test_deep_5000_task_acyclic_chain_remains_valid():
    tasks = [_task(i, [] if i == 0 else [f"T{i-1:05d}"]) for i in range(5000)]
    assert validate_plan_graph({"acceptance_criteria": ["valid"], "tasks": tasks}) == []


def test_verification_contract_symlink_fails_closed_even_when_target_is_valid():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ext:
        root = _repo(Path(td))
        target = Path(ext) / "verification.json"
        target.write_text(json.dumps({
            "schema_version": 1,
            "commands": {"test": ["echo outside"]},
        }))
        path = root / ".claude-auto" / "verification.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with pytest.raises(ValueError, match="repository-owned file, not a symlink"):
            load_declared_verification_commands(root)


def test_symlinked_control_directory_fails_closed():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ext:
        root = _repo(Path(td))
        external = Path(ext)
        (external / "verification.json").write_text(json.dumps({
            "schema_version": 1,
            "commands": {"test": ["echo outside"]},
        }))
        (root / ".claude-auto").symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match="repository-owned file, not a symlink"):
            load_declared_verification_commands(root)


def test_verification_contract_change_is_part_of_progress_fingerprint():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        (root / ".gitignore").write_text(".claude-auto/\n")
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "ignore control"], check=True)
        path = root / ".claude-auto" / "verification.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "commands": {"test": ["echo one"]}}))
        before = git_snapshot(root)
        fp1 = progress_fingerprint("CONTINUE", None, before, {})
        path.write_text(json.dumps({"schema_version": 1, "commands": {"test": ["echo two"]}}))
        after = git_snapshot(root)
        fp2 = progress_fingerprint("CONTINUE", None, after, {})
        assert before["verification_contract_sha256"] != after["verification_contract_sha256"]
        assert fp1 != fp2


def test_ignored_cache_noise_still_does_not_count_as_progress():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        (root / ".gitignore").write_text("cache/\n")
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "ignore cache"], check=True)
        cache = root / "cache" / "x"
        cache.parent.mkdir()
        cache.write_text("one")
        fp1 = progress_fingerprint("CONTINUE", None, git_snapshot(root), {})
        cache.write_text("two")
        fp2 = progress_fingerprint("CONTINUE", None, git_snapshot(root), {})
        assert fp1 == fp2


def test_systemd_execstart_escapes_percent_specifiers():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td) / "repo-%n-%h")
        unit = _service_unit_text(root, home=Path(td) / "home")
        exec_line = next(x for x in unit.splitlines() if x.startswith("ExecStart="))
        assert "repo-%%n-%%h" in exec_line
        description = next(x for x in unit.splitlines() if x.startswith("Description="))
        assert "repo-%%n-%%h" in description


def test_systemd_unit_rejects_newline_repository_path():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td) / "repo\nEnvironment=INJECTED=1")
        with pytest.raises(ValueError, match="control characters"):
            _service_unit_text(root, home=Path(td) / "home")


def test_systemd_ordinary_spaces_and_quotes_still_escape_correctly():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td) / 'repo with "quotes"')
        unit = _service_unit_text(root, home=Path(td) / "home space")
        exec_line = next(x for x in unit.splitlines() if x.startswith("ExecStart="))
        assert '\\"quotes\\"' in exec_line
        assert '--repo "' in exec_line


def test_nan_usage_cannot_disable_budget_breaker():
    args = SimpleNamespace(max_total_turns=0, max_wall_seconds=0, max_total_budget_usd=1.0)
    state = {"total_cost_usd": 0.5, "total_reported_turns": 0, "total_wall_seconds": 0}
    update_global_usage(state, {"usage": {"total_cost_usd": float("nan")}, "wall_seconds": 0})
    assert math.isfinite(float(state["total_cost_usd"]))
    reason = circuit_breaker_reason(args, state)
    assert reason and "accounting is invalid" in reason


def test_negative_usage_cannot_restore_budget():
    args = SimpleNamespace(max_budget_usd=2.0, max_total_budget_usd=5.0)
    state = {"total_cost_usd": 4.0, "total_reported_turns": 0, "total_wall_seconds": 0}
    before = effective_invocation_budget(args, state)
    update_global_usage(state, {"usage": {"total_cost_usd": -3.0}, "wall_seconds": 0})
    after = effective_invocation_budget(args, state)
    assert before == 1.0
    assert after == 0.0
    assert state["total_cost_usd"] == 4.0
    assert state["usage_accounting_invalid"] is True


def test_usage_parser_marks_invalid_numeric_accounting():
    parsed = usage_from_result({
        "usage": {"input_tokens": -5, "output_tokens": float("nan")},
        "total_cost_usd": float("inf"),
        "num_turns": -2,
        "duration_ms": -100,
    })
    assert parsed == {"_invalid_accounting": 1.0}


def test_usage_parser_keeps_valid_accounting_unchanged():
    parsed = usage_from_result({
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "total_cost_usd": 0.25,
        "num_turns": 2,
        "duration_ms": 123,
    })
    assert parsed["input_tokens"] == 10
    assert parsed["output_tokens"] == 4
    assert parsed["total_cost_usd"] == 0.25
    assert parsed["num_turns"] == 2
    assert parsed["duration_ms"] == 123
    assert "_invalid_accounting" not in parsed


def test_preexisting_nonfinite_usage_state_fails_closed():
    args = SimpleNamespace(max_total_turns=0, max_wall_seconds=0, max_total_budget_usd=10.0)
    state = {
        "total_cost_usd": float("nan"),
        "total_reported_turns": 0,
        "total_wall_seconds": 0,
    }
    reason = circuit_breaker_reason(args, state)
    assert reason and "accounting is invalid" in reason
    assert effective_invocation_budget(SimpleNamespace(max_budget_usd=2.0, max_total_budget_usd=10.0), state) == 0.0


def test_invalid_per_call_budget_fails_closed_to_zero():
    state = {"total_cost_usd": 1.0, "total_reported_turns": 0, "total_wall_seconds": 0}
    args = SimpleNamespace(max_budget_usd=float("nan"), max_total_budget_usd=10.0)
    assert effective_invocation_budget(args, state) == 0.0
