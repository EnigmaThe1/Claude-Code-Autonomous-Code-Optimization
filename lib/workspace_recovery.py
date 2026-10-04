from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from environment_policy import sanitised_subprocess_env


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=sanitised_subprocess_env(),
    )


def _normalise_relative_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("--path must be a non-empty repository-relative path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("--path must not contain control characters")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError("--path must stay inside the selected repository")
    return path


def _git_blob_digest_for_fd(fd: int, object_format: str) -> tuple[str, int]:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("cleanup target must be a regular file")
    if object_format == "sha1":
        h = hashlib.sha1()
    elif object_format == "sha256":
        h = hashlib.sha256()
    else:
        raise ValueError(f"unsupported Git object format: {object_format!r}")
    h.update(f"blob {st.st_size}\0".encode())
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest(), st.st_size


def cleanup_untracked_file(root: Path, raw_path: str, match_commit: str) -> dict[str, Any]:
    """Delete one proven-recoverable untracked file.

    The target must be repository-local, non-ignored, untracked, a regular
    non-symlink file, and byte-identical (Git blob identity) to the same path in
    a descendant commit. This gives autonomous cleanup a deterministic recovery
    path without granting broad rm authority.
    """
    root = root.expanduser().resolve()
    rel = _normalise_relative_path(raw_path)
    candidate = root / rel
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("cleanup target parent must resolve inside the repository") from exc

    try:
        lst = candidate.lstat()
    except FileNotFoundError as exc:
        raise ValueError("cleanup target does not exist") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise ValueError("cleanup target must not be a symlink")
    if not stat.S_ISREG(lst.st_mode):
        raise ValueError("cleanup target must be a regular file")

    rel_git = rel.as_posix()
    tracked = _git(root, "ls-files", "--error-unmatch", "--", rel_git)
    if tracked.returncode == 0:
        raise ValueError("cleanup target is tracked; refusing deletion")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", rel_git)
    rows = {x.strip() for x in untracked.stdout.splitlines() if x.strip()}
    if untracked.returncode != 0 or rel_git not in rows:
        raise ValueError("cleanup target must be a visible non-ignored untracked file")

    commit_cp = _git(root, "rev-parse", "--verify", f"{match_commit}^{{commit}}")
    if commit_cp.returncode != 0:
        raise ValueError("match commit is not a valid local Git commit")
    commit = commit_cp.stdout.strip()

    # Require the recovery commit to be forward from current HEAD. This matches
    # aborted fast-forward/promotion recovery and prevents arbitrary unrelated
    # object-store content from authorising deletion.
    ancestor = _git(root, "merge-base", "--is-ancestor", "HEAD", commit)
    if ancestor.returncode != 0:
        raise ValueError("match commit must be a descendant of current HEAD")

    blob_cp = _git(root, "rev-parse", "--verify", f"{commit}:{rel_git}")
    if blob_cp.returncode != 0:
        raise ValueError("match commit does not contain the cleanup path")
    expected_blob = blob_cp.stdout.strip().lower()

    fmt_cp = _git(root, "rev-parse", "--show-object-format")
    if fmt_cp.returncode != 0:
        raise ValueError("unable to determine Git object format")
    object_format = fmt_cp.stdout.strip().lower()

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(candidate, flags)
    try:
        actual_blob, size = _git_blob_digest_for_fd(fd, object_format)
        before = os.fstat(fd)
        current = candidate.lstat()
        if (
            before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or before.st_size != current.st_size
        ):
            raise ValueError("cleanup target changed while being verified")
        if actual_blob.lower() != expected_blob:
            raise ValueError("cleanup target does not match the recoverable Git blob")
        candidate.unlink()
    finally:
        os.close(fd)

    return {
        "status": "deleted",
        "path": rel_git,
        "match_commit": commit,
        "blob": expected_blob,
        "size": size,
    }


def cleanup_untracked_action(args: Any, *, find_repo_root) -> int:
    try:
        root = find_repo_root(getattr(args, "repo", None))
        result = cleanup_untracked_file(root, args.path, args.match_commit)
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0

def _git_output_hash(root: Path, *args: str) -> str:
    cp = _git(root, *args)
    if cp.returncode != 0:
        raise ValueError("unable to capture Git working-state evidence")
    return hashlib.sha256(cp.stdout.encode("utf-8", errors="surrogateescape")).hexdigest()


def _visible_untracked(root: Path) -> set[str]:
    cp = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if cp.returncode != 0:
        raise ValueError("unable to enumerate untracked files")
    return {item for item in cp.stdout.split("\0") if item}


def _local_wip_signature(root: Path) -> dict[str, Any]:
    untracked = sorted(_visible_untracked(root))
    entries: list[dict[str, Any]] = []
    for rel in untracked:
        path = root / rel
        try:
            if path.is_symlink():
                entries.append({"path": rel, "kind": "symlink", "target": os.readlink(path)})
            elif path.is_file():
                h = hashlib.sha256()
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                entries.append({"path": rel, "kind": "file", "sha256": h.hexdigest(), "size": path.stat().st_size})
            else:
                entries.append({"path": rel, "kind": "other"})
        except OSError:
            entries.append({"path": rel, "kind": "unreadable"})
    return {
        "worktree_diff": _git_output_hash(root, "diff", "--no-ext-diff", "--binary"),
        "index_diff": _git_output_hash(root, "diff", "--no-ext-diff", "--cached", "--binary"),
        "untracked": entries,
    }


def _changed_paths(root: Path, base: str, target: str) -> set[str]:
    cp = _git(root, "diff", "--name-only", "-z", base, target, "--")
    if cp.returncode != 0:
        raise ValueError("unable to determine fast-forward changed paths")
    return {item for item in cp.stdout.split("\0") if item}


def _dirty_paths(root: Path) -> set[str]:
    out: set[str] = set()
    for args in (
        ("diff", "--name-only", "-z", "--"),
        ("diff", "--cached", "--name-only", "-z", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        cp = _git(root, *args)
        if cp.returncode != 0:
            raise ValueError("unable to determine local dirty paths")
        out.update(item for item in cp.stdout.split("\0") if item)
    return out


def promote_fast_forward(root: Path, target_commit: str) -> dict[str, Any]:
    """Fast-forward the current branch while preserving pre-existing local WIP."""
    root = root.expanduser().resolve()
    branch_cp = _git(root, "branch", "--show-current")
    if branch_cp.returncode != 0 or not branch_cp.stdout.strip():
        raise ValueError("promotion requires a named current branch")
    branch = branch_cp.stdout.strip()

    head_cp = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    target_cp = _git(root, "rev-parse", "--verify", f"{target_commit}^{{commit}}")
    if head_cp.returncode != 0 or target_cp.returncode != 0:
        raise ValueError("HEAD and target must be valid local commits")
    before_head = head_cp.stdout.strip()
    target = target_cp.stdout.strip()
    if target == before_head:
        return {
            "status": "already-promoted",
            "branch": branch,
            "before_head": before_head,
            "head": target,
            "cleaned_residue": [],
        }

    ancestor = _git(root, "merge-base", "--is-ancestor", before_head, target)
    if ancestor.returncode != 0:
        raise ValueError("target commit is not a descendant of current HEAD; refusing non-fast-forward promotion")

    changed = _changed_paths(root, before_head, target)
    dirty = _dirty_paths(root)
    overlap = sorted(changed & dirty)
    if overlap:
        raise ValueError(
            "target overlaps pre-existing local WIP; refusing promotion: "
            + ", ".join(overlap[:40])
        )

    before_wip = _local_wip_signature(root)
    before_untracked = _visible_untracked(root)
    cp = _git(root, "merge", "--ff-only", target)
    if cp.returncode != 0:
        now_head = _git(root, "rev-parse", "HEAD").stdout.strip()
        if now_head != before_head:
            raise ValueError(
                "fast-forward command failed after HEAD moved; manual repository recovery is required"
            )
        cleaned: list[str] = []
        after_untracked = _visible_untracked(root)
        for rel in sorted(after_untracked - before_untracked):
            try:
                cleanup_untracked_file(root, rel, target)
                cleaned.append(rel)
            except (OSError, ValueError):
                pass
        if _local_wip_signature(root) != before_wip:
            raise ValueError(
                "fast-forward failed and local WIP changed; automatic recovery stopped"
            )
        detail = (cp.stderr or cp.stdout or "git merge --ff-only failed").strip()
        suffix = f"; cleaned recoverable residue: {', '.join(cleaned)}" if cleaned else ""
        raise ValueError(detail[:1600] + suffix)

    after_head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if after_head != target:
        raise ValueError("fast-forward returned success but HEAD is not the exact target commit")
    if _local_wip_signature(root) != before_wip:
        raise ValueError("fast-forward moved HEAD but did not preserve pre-existing local WIP exactly")

    return {
        "status": "promoted",
        "branch": branch,
        "before_head": before_head,
        "head": after_head,
        "cleaned_residue": [],
    }


def promote_ff_action(args: Any, *, find_repo_root) -> int:
    try:
        root = find_repo_root(getattr(args, "repo", None))
        result = promote_fast_forward(root, args.sha)
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0

