from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from git_trust import (
    configure_trusted_excludes,
    trusted_git_env,
)
from promotion_policy import (
    REPOSITORY_PLANNING_REPAIR_CONTRACT,
    configure_promotion_policy,
    record_promotion_attestation,
)
from planning_repair import configure_planning_repair
from workspace_recovery import (
    _visible_untracked,
    promote_fast_forward,
)


def _run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-q", "-b", "main", str(path))
    _run("git", "-C", str(path), "config", "user.email", "test@example.invalid")
    _run("git", "-C", str(path), "config", "user.name", "Test")
    (path / "base.txt").write_text("base\n")
    _run("git", "-C", str(path), "add", "base.txt")
    _run("git", "-C", str(path), "commit", "-qm", "base")
    return path


def _commit(root: Path, name: str, content: str) -> str:
    (root / name).write_text(content)
    _run("git", "-C", str(root), "add", name)
    _run("git", "-C", str(root), "commit", "-qm", f"add {name}")
    return _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()


def _evidence(label: str) -> str:
    import hashlib
    return hashlib.sha256(label.encode()).hexdigest()


def test_trusted_git_reconstruction_discards_poison_and_rebuilds_only_package_config(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        excludes = Path(td) / "trusted-excludes"
        excludes.write_text(".bashrc\n.mcp.json\n")
        excludes.chmod(0o600)
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_trusted_excludes(root, excludes)

        env = trusted_git_env(root, {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.filemode",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_PARAMETERS": "'core.ignorecase=true'",
            "GIT_SSH_COMMAND": "ssh test",
        })

        assert env["GIT_CONFIG_COUNT"] == "5"
        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert set(pairs) == {
            "core.excludesFile",
            "core.hooksPath",
            "core.fsmonitor",
            "fetch.recurseSubmodules",
            "submodule.recurse",
        }
        trusted_copy = Path(pairs["core.excludesFile"])
        assert trusted_copy != excludes.resolve()
        assert trusted_copy.read_text() == excludes.read_text()
        assert pairs["core.hooksPath"] == os.devnull
        assert pairs["core.fsmonitor"] == "false"
        assert pairs["fetch.recurseSubmodules"] == "false"
        assert pairs["submodule.recurse"] == "false"
        assert env["GIT_SSH_COMMAND"] == "ssh test"
        assert "core.filemode" not in pairs
        assert "core.ignorecase" not in pairs


def test_trusted_excludes_hide_only_harness_stubs_but_real_wip_remains_visible(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        base = Path(td)
        root = _repo(base / "repo")
        excludes = base / "trusted-excludes"
        excludes.write_text(".bashrc\n.mcp.json\n")
        excludes.chmod(0o600)
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        configure_trusted_excludes(root, excludes)

        (root / ".bashrc").write_text("sandbox stub\n")
        (root / ".mcp.json").write_text("{}\n")
        (root / "real-untracked.txt").write_text("real work\n")

        assert _visible_untracked(root) == {"real-untracked.txt"}


def test_trusted_excludes_reject_repository_owned_policy_file(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        candidate = root / ".trusted-excludes"
        candidate.write_text("*.tmp\n")
        candidate.chmod(0o600)
        with pytest.raises(ValueError, match="outside the repository"):
            configure_trusted_excludes(root, candidate)


def test_protected_local_promotion_requires_exact_target_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        target = _commit(root, "target.txt", "target\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)

        configure_promotion_policy(root, "planning-repair")
        with pytest.raises(ValueError, match="attestation required"):
            promote_fast_forward(root, target)

        record_promotion_attestation(
            root,
            target_sha=target,
            contract="planning-repair",
            verifier="independent-test-verifier",
            evidence_sha256=_evidence("target"),
            summary="exact target verified",
        )
        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert result["head"] == target
        assert result["attestation"]["target_sha"] == target


def test_attestation_for_different_sha_cannot_authorise_target(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        first = _commit(root, "one.txt", "one\n")
        second = _commit(root, "two.txt", "two\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)
        configure_promotion_policy(root, "planning-repair")
        record_promotion_attestation(
            root,
            target_sha=first,
            contract="planning-repair",
            verifier="independent-test-verifier",
            evidence_sha256=_evidence("first"),
        )
        with pytest.raises(ValueError, match="attestation required"):
            promote_fast_forward(root, second)


def test_remote_promotion_checks_expected_base_and_is_idempotent(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        base_dir = Path(td)
        remote = base_dir / "remote.git"
        _run("git", "init", "--bare", "-q", str(remote))
        root = _repo(base_dir / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        _run("git", "-C", str(root), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(root), "push", "-q", "-u", "origin", "main")
        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()

        target = _commit(root, "target.txt", "target\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)
        configure_promotion_policy(root, "planning-repair")
        record_promotion_attestation(
            root,
            target_sha=target,
            contract="planning-repair",
            verifier="independent-test-verifier",
            evidence_sha256=_evidence("remote-target"),
        )

        result = promote_fast_forward(
            root,
            target,
            remote="origin",
            remote_branch="main",
            expected_remote=base,
        )
        assert result["status"] == "promoted-remote"
        assert result["head"] == target
        assert result["remote_head"] == target

        retry = promote_fast_forward(
            root,
            target,
            remote="origin",
            remote_branch="main",
            expected_remote=base,
        )
        assert retry["status"] == "already-remote"
        assert retry["remote_head"] == target


def test_remote_promotion_refuses_remote_that_moved_to_unexpected_commit(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        base_dir = Path(td)
        remote = base_dir / "remote.git"
        _run("git", "init", "--bare", "-q", str(remote))
        root = _repo(base_dir / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        _run("git", "-C", str(root), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(root), "push", "-q", "-u", "origin", "main")
        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()

        target = _commit(root, "target.txt", "target\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)
        competing = _commit(root, "competing.txt", "competing\n")
        _run("git", "-C", str(root), "push", "-q", "origin", f"{competing}:refs/heads/main")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)

        configure_promotion_policy(root, "planning-repair")
        record_promotion_attestation(
            root,
            target_sha=target,
            contract="planning-repair",
            verifier="independent-test-verifier",
            evidence_sha256=_evidence("target"),
        )

        with pytest.raises(ValueError, match="remote branch moved unexpectedly"):
            promote_fast_forward(
                root,
                target,
                remote="origin",
                remote_branch="main",
                expected_remote=base,
            )


def test_remote_push_lost_response_reconciles_remote_truth(monkeypatch):
    import workspace_recovery as wr

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        base_dir = Path(td)
        remote = base_dir / "remote.git"
        _run("git", "init", "--bare", "-q", str(remote))
        root = _repo(base_dir / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        _run("git", "-C", str(root), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(root), "push", "-q", "-u", "origin", "main")
        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()

        target = _commit(root, "target.txt", "target\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)
        configure_promotion_policy(root, "planning-repair")
        record_promotion_attestation(
            root,
            target_sha=target,
            contract="planning-repair",
            verifier="independent-test-verifier",
            evidence_sha256=_evidence("lost-response"),
        )

        original_git = wr._git

        def flaky_git(repo: Path, *args: str):
            if args and args[0] == "push":
                successful = original_git(repo, *args)
                assert successful.returncode == 0, successful.stderr
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    successful.stdout,
                    "simulated transport response loss after server accepted push",
                )
            return original_git(repo, *args)

        monkeypatch.setattr(wr, "_git", flaky_git)
        result = wr.promote_fast_forward(
            root,
            target,
            remote="origin",
            remote_branch="main",
            expected_remote=base,
        )
        assert result["status"] == "promoted-remote-reconciled"
        assert result["head"] == target
        assert result["remote_head"] == target


def test_promotion_uses_same_trusted_excludes_view_and_preserves_real_wip(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        base_dir = Path(td)
        root = _repo(base_dir / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        excludes = base_dir / "trusted-excludes"
        excludes.write_text(".bashrc\n.mcp.json\n")
        excludes.chmod(0o600)
        configure_trusted_excludes(root, excludes)

        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        target = _commit(root, "target.txt", "target\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)

        (root / ".bashrc").write_text("sandbox stub\n")
        (root / ".mcp.json").write_text("{}\n")
        (root / "real-untracked.txt").write_text("real work\n")

        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert (root / "real-untracked.txt").read_text() == "real work\n"
        assert (root / ".bashrc").exists()
        assert (root / ".mcp.json").exists()
        assert _visible_untracked(root) == {"real-untracked.txt"}


def test_generic_promote_ff_cannot_bypass_canonical_plan_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n\n1. Original.\n")
        _run("git", "-C", str(root), "add", "IMPLEMENTATION_PLAN.md")
        _run("git", "-C", str(root), "commit", "-qm", "add plan")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")

        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        (root / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n\n1. Repaired.\n")
        _run("git", "-C", str(root), "add", "IMPLEMENTATION_PLAN.md")
        _run("git", "-C", str(root), "commit", "-qm", "repair plan")
        target = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)

        with pytest.raises(ValueError, match="attestation required"):
            promote_fast_forward(root, target)

        record_promotion_attestation(
            root,
            target_sha=target,
            contract=REPOSITORY_PLANNING_REPAIR_CONTRACT,
            verifier="independent-planning-verifier",
            evidence_sha256=_evidence("plan"),
        )
        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert result["attestation"]["contract"] == REPOSITORY_PLANNING_REPAIR_CONTRACT


def test_non_plan_fast_forward_remains_available_without_planning_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        (root / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n")
        _run("git", "-C", str(root), "add", "IMPLEMENTATION_PLAN.md")
        _run("git", "-C", str(root), "commit", "-qm", "add plan")
        configure_planning_repair(root, canonical_plan="IMPLEMENTATION_PLAN.md")

        base = _run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
        target = _commit(root, "ordinary.txt", "ordinary\n")
        _run("git", "-C", str(root), "reset", "--hard", "-q", base)
        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert result["attestation"] is None


def test_operator_excludes_are_copied_so_later_source_mutation_cannot_change_broker_view(monkeypatch):
    from git_trust import trusted_git_config

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        source = Path(td) / "operator-excludes"
        source.write_text(".bashrc\n")
        source.chmod(0o600)
        configure_trusted_excludes(root, source)

        before = dict(trusted_git_config(root))
        trusted_copy = Path(before["core.excludesFile"])
        assert trusted_copy.read_text() == ".bashrc\n"

        source.write_text(".bashrc\nreal-untracked.txt\n")
        after = dict(trusted_git_config(root))
        assert after["core.excludesFile"] == str(trusted_copy)
        assert trusted_copy.read_text() == ".bashrc\n"

        (root / ".bashrc").write_text("stub\n")
        (root / "real-untracked.txt").write_text("real\n")
        assert _visible_untracked(root) == {"real-untracked.txt"}


def test_default_broker_view_overrides_global_excludes_with_empty_package_file(monkeypatch):
    from git_trust import trusted_git_env

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
        root = _repo(Path(td) / "repo")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state_td)
        global_excludes = Path(td) / "global-excludes"
        global_excludes.write_text("real-untracked.txt\n")
        _run("git", "-C", str(root), "config", "core.excludesFile", str(global_excludes))

        (root / "real-untracked.txt").write_text("real\n")
        env = trusted_git_env(root)
        assert env["GIT_CONFIG_COUNT"] == "5"
        assert _visible_untracked(root) == {"real-untracked.txt"}
