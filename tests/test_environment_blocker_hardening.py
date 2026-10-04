from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from settings_policy import make_settings
from supervisor_support import resolve_session_settings
from workspace_recovery import cleanup_untracked_file


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(path: Path, *, ignore_recovery: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("baseline\n")
    if ignore_recovery:
        (path / ".gitignore").write_text("recovery.txt\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _forward_commit_with_file(
    root: Path,
    rel: str = "recovery.txt",
    content: bytes = b"recoverable\n",
    *,
    force_add: bool = False,
) -> tuple[str, bytes]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    add = ["add"]
    if force_add:
        add.append("-f")
    add += ["--", rel]
    _git(root, *add)
    _git(root, "commit", "-qm", "forward repair")
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "reset", "--hard", "-q", "HEAD^")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return sha, content


def test_headless_balanced_defaults_hermetic():
    assert resolve_session_settings(None, "balanced", headless=True) == "hermetic"


def test_headless_isolated_full_defaults_hermetic():
    assert resolve_session_settings(None, "isolated-full", headless=True) == "hermetic"


def test_interactive_balanced_remains_compatibility():
    assert resolve_session_settings(None, "balanced", headless=False) == "compatibility"


def test_headless_strict_remains_compatibility_by_default():
    assert resolve_session_settings(None, "strict", headless=True) == "compatibility"


def test_explicit_compatibility_overrides_headless_balanced_default():
    assert resolve_session_settings("compatibility", "balanced", headless=True) == "compatibility"


def test_balanced_settings_preapprove_only_safe_cleanup_helper():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
        root = _repo(Path(td))
        settings = make_settings(
            Path(sd),
            "external",
            "balanced",
            {"repo_root": str(root), "container_files": [], "languages": []},
        )
        allow = settings["permissions"]["allow"]
        assert "Bash(claude-auto cleanup-untracked *)" in allow
        assert "Bash(rm *)" not in allow
        assert "Bash(git *)" not in allow


def test_cleanup_exact_recoverable_untracked_file_succeeds():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        repair, content = _forward_commit_with_file(root)
        target = root / "recovery.txt"
        assert target.read_bytes() == content
        result = cleanup_untracked_file(root, "recovery.txt", repair)
        assert result["status"] == "deleted"
        assert result["match_commit"] == repair
        assert not target.exists()


def test_cleanup_refuses_changed_content():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        repair, _ = _forward_commit_with_file(root)
        target = root / "recovery.txt"
        target.write_text("different\n")
        with pytest.raises(ValueError, match="does not match"):
            cleanup_untracked_file(root, "recovery.txt", repair)
        assert target.exists()


def test_cleanup_refuses_tracked_file():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with pytest.raises(ValueError, match="tracked"):
            cleanup_untracked_file(root, "tracked.txt", head)
        assert (root / "tracked.txt").exists()


def test_cleanup_refuses_ignored_untracked_file():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td), ignore_recovery=True)
        repair, _ = _forward_commit_with_file(root, force_add=True)
        with pytest.raises(ValueError, match="visible non-ignored untracked"):
            cleanup_untracked_file(root, "recovery.txt", repair)
        assert (root / "recovery.txt").exists()


def test_cleanup_refuses_symlink():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        repair, _ = _forward_commit_with_file(root)
        target = root / "recovery.txt"
        target.unlink()
        target.symlink_to("tracked.txt")
        with pytest.raises(ValueError, match="must not be a symlink"):
            cleanup_untracked_file(root, "recovery.txt", repair)
        assert target.is_symlink()


@pytest.mark.parametrize("bad", ["../outside.txt", "/tmp/outside.txt", ".", "a/../../outside"])
def test_cleanup_refuses_outside_or_ambiguous_path(bad: str):
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with pytest.raises(ValueError):
            cleanup_untracked_file(root, bad, head)


def test_cleanup_refuses_non_descendant_commit():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        repair, content = _forward_commit_with_file(root)
        # Create a sibling commit so current HEAD is no longer an ancestor of
        # the forward-repair commit.
        (root / "sibling.txt").write_text("sibling\n")
        _git(root, "add", "sibling.txt")
        _git(root, "commit", "-qm", "sibling")
        (root / "recovery.txt").write_bytes(content)
        assert _git(root, "rev-parse", "HEAD").stdout.strip() != base
        with pytest.raises(ValueError, match="descendant"):
            cleanup_untracked_file(root, "recovery.txt", repair)


def test_cleanup_refuses_commit_missing_same_path():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        (root / "forward.txt").write_text("x\n")
        _git(root, "add", "forward.txt")
        _git(root, "commit", "-qm", "forward without target")
        forward = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "reset", "--hard", "-q", base)
        (root / "recovery.txt").write_text("recoverable\n")
        with pytest.raises(ValueError, match="does not contain"):
            cleanup_untracked_file(root, "recovery.txt", forward)


def test_cleanup_streams_large_recoverable_file():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        content = b"A" * (21 * 1024 * 1024 + 17)
        repair, _ = _forward_commit_with_file(root, content=content)
        result = cleanup_untracked_file(root, "recovery.txt", repair)
        assert result["size"] == len(content)
        assert not (root / "recovery.txt").exists()


def test_cleanup_then_fast_forward_matches_real_abort_recovery_shape():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        repair, content = _forward_commit_with_file(
            root,
            rel="docs/implementation-evidence/repair.md",
            content=b"verified evidence\n",
        )
        target = root / "docs/implementation-evidence/repair.md"
        assert target.read_bytes() == content
        cleanup_untracked_file(root, "docs/implementation-evidence/repair.md", repair)
        cp = _git(root, "merge", "--ff-only", repair, check=False)
        assert cp.returncode == 0, cp.stderr
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == repair
        assert target.read_bytes() == content
