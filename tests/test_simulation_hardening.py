from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from control_plane import _completion_source_unchanged
from planning_support import validate_progress_checkpoint
from repo_profile import (
    MAX_DECLARED_VERIFICATION_COMMANDS,
    detect_commands,
    load_declared_verification_commands,
)
from repo_runtime import git_snapshot
from verification import _verification_commands


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "Test"], check=True)
    (d / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "baseline"], check=True)
    return d


def _write_contract(root: Path, commands: list[str]) -> Path:
    path = root / ".claude-auto" / "verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "commands": {"test": commands},
    }))
    return path


def test_complete_progress_requires_all_verification_values_pass():
    plan = {
        "acceptance_criteria": ["done"],
        "tasks": [
            {"id": "T1", "title": "one", "depends_on": [], "verification": ["unit"], "risk": "low"},
            {"id": "T2", "title": "two", "depends_on": ["T1"], "verification": ["integration"], "risk": "medium"},
        ],
    }

    good = {
        "completed_task_ids": ["T1", "T2"],
        "remaining_task_ids": [],
        "verification": {"unit": "PASS", "integration": "PASS"},
    }
    assert validate_progress_checkpoint(plan, good, require_partition=True) == []

    failed = {
        "completed_task_ids": ["T1", "T2"],
        "remaining_task_ids": [],
        "verification": {"unit": "PASS", "integration": "FAIL"},
    }
    errors = validate_progress_checkpoint(plan, failed, require_partition=True)
    assert any("must be PASS, got FAIL" in x for x in errors)

    unknown = {
        "completed_task_ids": ["T1", "T2"],
        "remaining_task_ids": [],
        "verification": {"unit": "PASS", "integration": "UNKNOWN"},
    }
    errors = validate_progress_checkpoint(plan, unknown, require_partition=True)
    assert any("must be PASS, got UNKNOWN" in x for x in errors)


def test_noncomplete_progress_can_report_fail_or_unknown():
    plan = {
        "acceptance_criteria": ["done"],
        "tasks": [
            {"id": "T1", "title": "one", "depends_on": [], "verification": ["unit"], "risk": "low"},
            {"id": "T2", "title": "two", "depends_on": ["T1"], "verification": ["integration"], "risk": "medium"},
        ],
    }
    checkpoint = {
        "completed_task_ids": ["T1"],
        "remaining_task_ids": ["T2"],
        "verification": {"unit": "PASS", "integration": "UNKNOWN"},
    }
    assert validate_progress_checkpoint(plan, checkpoint, require_partition=False) == []


def test_all_declared_mandatory_commands_survive_profile_and_final_gate():
    root = Path(tempfile.mkdtemp())
    commands = [f"python3 check_{i:02d}.py" for i in range(60)]
    _write_contract(root, commands)

    declared = load_declared_verification_commands(root)
    assert declared["test"] == commands

    hints = detect_commands(root, set())
    for command in commands:
        assert command in hints["test"]

    final = [cmd for category, cmd in _verification_commands({
        "repo_root": str(root),
        "build_test_hints": hints,
    }) if category == "test"]
    assert final[:60] == commands
    assert all(command in final for command in commands)


def test_declared_contract_limit_fails_closed_instead_of_truncating():
    root = Path(tempfile.mkdtemp())
    commands = [f"echo check-{i}" for i in range(MAX_DECLARED_VERIFICATION_COMMANDS + 1)]
    _write_contract(root, commands)
    with pytest.raises(ValueError, match="declares more than"):
        load_declared_verification_commands(root)


def test_ignored_verification_contract_is_part_of_completion_identity():
    root = _repo()
    (root / ".gitignore").write_text(".claude-auto/\n")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "ignore runtime dir"], check=True)

    contract = _write_contract(root, ["python3 check_a.py"])
    before = git_snapshot(root)
    contract.write_text(json.dumps({
        "schema_version": 1,
        "commands": {"test": ["python3 check_b.py"]},
    }))
    after = git_snapshot(root)

    assert before["verification_contract_sha256"] != after["verification_contract_sha256"]
    assert not _completion_source_unchanged(before, after)


def test_large_untracked_content_change_is_exact_even_if_metadata_restored():
    root = _repo()
    big = root / "large-source.bin"
    size = 21 * 1024 * 1024
    with big.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"A")

    st = big.stat()
    before = git_snapshot(root)

    with big.open("r+b") as fh:
        fh.seek(size - 1)
        fh.write(b"B")
    os.utime(big, ns=(st.st_atime_ns, st.st_mtime_ns))

    after = git_snapshot(root)
    assert before["untracked_sha256"] != after["untracked_sha256"]
    assert not _completion_source_unchanged(before, after)


def test_missing_and_reappearing_contract_changes_completion_identity():
    root = _repo()
    before = git_snapshot(root)
    contract = _write_contract(root, ["echo check"])
    with_contract = git_snapshot(root)
    contract.unlink()
    after = git_snapshot(root)

    assert before["verification_contract_sha256"] is None
    assert with_contract["verification_contract_sha256"] is not None
    assert not _completion_source_unchanged(before, with_contract)
    assert after["verification_contract_sha256"] is None
