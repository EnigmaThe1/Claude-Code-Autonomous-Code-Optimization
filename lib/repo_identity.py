# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from process_runner import run
from runtime_paths import data_home, ensure_private_dir, utcnow
from state_store import json_dump, load_json


def find_repo_root(start: str | Path | None = None) -> Path:
    p = Path(start or os.getcwd()).expanduser().resolve()
    if p.is_file():
        p = p.parent
    cp = run(["git", "-C", str(p), "rev-parse", "--show-toplevel"])
    if cp.returncode == 0 and cp.stdout.strip():
        return Path(cp.stdout.strip()).resolve()

    markers = {
        "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml",
        "build.gradle", "build.gradle.kts", "Makefile", "justfile", ".git"
    }
    cur = p
    while True:
        if any((cur / m).exists() for m in markers):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise SystemExit(f"Could not identify a repository/project root from {p}. Pass --repo PATH explicitly.")


def _filesystem_birth_marker(path: Path) -> str | None:
    """Best-effort filesystem creation marker stable across path renames.

    Linux stat exposes birth time through %w on filesystems that support it.
    Other platforms may expose st_birthtime directly.  This complements inode
    identity so delete/recreate at the same path cannot inherit old autonomy
    state merely because the filesystem recycled inode numbers.
    """
    try:
        st = path.stat()
    except OSError:
        return None

    birth_ns = getattr(st, "st_birthtime_ns", None)
    if birth_ns is not None:
        return f"birth-ns:{int(birth_ns)}"
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return f"birth:{birth!r}"

    stat_bin = shutil.which("stat")
    if stat_bin:
        # GNU/coreutils %w includes sub-second precision when available. It
        # prints '-' on filesystems without birth-time support.
        cp = run([stat_bin, "-c", "%w", str(path)], timeout=5)
        value = cp.stdout.strip() if cp.returncode == 0 else ""
        if value and value != "-":
            return "birth-stat:" + value
    return None


def repository_identity(root: Path) -> dict[str, Any]:
    """Identify one local Git work unit without keying identity on mutable history/remotes.

    Stable identity is derived from the local Git repository instance plus the
    worktree metadata identity.  Mutable evidence such as origin URLs and root
    commits is retained for reconciliation/diagnostics but is deliberately not
    part of the durable-state key.
    """
    root = root.resolve()
    common = run(["git", "-C", str(root), "rev-parse", "--git-common-dir"])
    git_dir = run(["git", "-C", str(root), "rev-parse", "--git-dir"])
    common_path: Path | None = None
    git_dir_path: Path | None = None
    common_inode = None
    worktree_inode = None
    common_birth = None
    worktree_birth = None

    if common.returncode == 0 and common.stdout.strip():
        cp = Path(common.stdout.strip())
        common_path = (root / cp).resolve() if not cp.is_absolute() else cp.resolve()
        try:
            sig = []
            for meta_path in (common_path, common_path / "objects", common_path / "refs"):
                st = meta_path.stat()
                sig.append(f"{st.st_dev}:{st.st_ino}")
            common_inode = "|".join(sig)
            common_birth = _filesystem_birth_marker(common_path)
        except OSError:
            common_inode = None
            common_birth = None

    if git_dir.returncode == 0 and git_dir.stdout.strip():
        gp = Path(git_dir.stdout.strip())
        git_dir_path = (root / gp).resolve() if not gp.is_absolute() else gp.resolve()
        try:
            st = git_dir_path.stat()
            worktree_inode = f"{st.st_dev}:{st.st_ino}"
            worktree_birth = _filesystem_birth_marker(git_dir_path)
        except OSError:
            worktree_inode = None
            worktree_birth = None

    instance_marker = None
    if common_path is not None:
        try:
            marker = common_path / "description"
            st = marker.stat()
            # the current implementation durable identity must not move when an editor rewrites the
            # repository description metadata.  The marker contributes only its
            # filesystem identity; mutable times/size remain diagnostic at most.
            instance_marker = f"{st.st_dev}:{st.st_ino}"
        except OSError:
            pass

    # Distinguish linked worktrees while keeping the main checkout stable across
    # path moves.  Git stores linked worktree metadata beneath
    # <common>/worktrees/<name>; that relative metadata identity is stable for the
    # lifetime of the worktree and does not depend on the checkout path.
    worktree_key = "main"
    if common_path is not None and git_dir_path is not None and git_dir_path != common_path:
        try:
            worktree_key = str(git_dir_path.relative_to(common_path))
        except ValueError:
            worktree_key = worktree_inode or str(git_dir_path)

    stable_material: dict[str, Any] = {
        # Directory identity is the durable local-repository signal.  Do not key
        # on ordinary files inside .git (for example "description"), because
        # editors commonly replace those files atomically and therefore change
        # their inode without creating a new repository.
        "git_common_inode": common_inode,
        "git_common_birth": common_birth,
        "worktree_key": worktree_key,
        "worktree_inode": worktree_inode,
        "worktree_birth": worktree_birth,
    }

    # Mutable corroborating evidence is intentionally excluded from the key.
    roots = run(["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"])
    root_commits = sorted(
        x for x in roots.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", x)
    ) if roots.returncode == 0 else []
    origin = run(["git", "-C", str(root), "remote", "get-url", "origin"])
    origin_url = origin.stdout.strip() if origin.returncode == 0 and origin.stdout.strip() else None

    if not any(stable_material.values()):
        stable_material["path_fallback"] = str(root)

    key = hashlib.sha256(
        json.dumps(stable_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    material = {
        **stable_material,
        "git_instance_marker_evidence": instance_marker,
        "root_commits_evidence": root_commits,
        "origin_evidence": origin_url,
    }
    return {
        "key": key,
        "material": material,
        "current_root": str(root),
        "git_common_dir": str(common_path) if common_path else None,
        "git_dir": str(git_dir_path) if git_dir_path else None,
    }

def repo_id(root: Path) -> str:
    return repository_identity(root)["key"]


def repo_state_dir(root: Path) -> Path:
    return data_home() / "repos" / repo_id(root)


def _legacy_repo_identity(root: Path) -> dict[str, Any]:
    """Reconstruct the legacy pre-1.0 implementation's mutable identity only for upgrade reconciliation."""
    root = root.resolve()
    common = run(["git", "-C", str(root), "rev-parse", "--git-common-dir"])
    common_path: Path | None = None
    inode = None
    instance_marker = None
    if common.returncode == 0 and common.stdout.strip():
        cp = Path(common.stdout.strip())
        common_path = (root / cp).resolve() if not cp.is_absolute() else cp.resolve()
        try:
            sig = []
            for meta_path in (common_path, common_path / "objects", common_path / "refs"):
                st = meta_path.stat()
                sig.append(f"{st.st_dev}:{st.st_ino}")
            inode = "|".join(sig)
        except OSError:
            inode = None
        try:
            st = (common_path / "description").stat()
            instance_marker = f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            pass
    roots = run(["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"])
    root_commits = sorted(
        x for x in roots.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", x)
    ) if roots.returncode == 0 else []
    origin = run(["git", "-C", str(root), "remote", "get-url", "origin"])
    origin_url = origin.stdout.strip() if origin.returncode == 0 and origin.stdout.strip() else None
    material: dict[str, Any] = {
        "git_common_inode": inode,
        "git_instance_marker": instance_marker,
        "root_commits": root_commits,
        "origin": origin_url,
    }
    if not inode and not instance_marker and not root_commits and not origin_url:
        material["path_fallback"] = str(root)
    key = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return {"key": key, "material": material, "current_root": str(root)}


def _unchecked_state_json(path: Path) -> dict[str, Any]:
    """Read candidate legacy state without recovery writes or side effects."""
    for name in ("state.json", "state.prev.json"):
        p = path / name
        try:
            obj = json.loads(p.read_text())
            if isinstance(obj, dict):
                return obj
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
    return {}


def _legacy_supervisor_is_active(path: Path) -> bool:
    try:
        meta = json.loads((path / "supervisor-lease.json").read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(meta, dict) or not meta.get("active"):
        return False
    host = str(meta.get("hostname") or "")
    if host and host != socket.gethostname():
        # On shared storage we cannot prove a foreign-host lease is stale.
        return True
    try:
        pid = int(meta.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _migrate_legacy_state_if_needed(root: Path, target: Path) -> Path | None:
    """Move one unambiguous the legacy pre-1.0 implementation state directory onto the the current implementation stable identity.

    This runs only while the current implementation's external per-work-unit guard is held.  Exact the legacy pre-1.0 implementation
    identity is tried first.  If mutable origin/history already changed, a bounded
    scan reconciles by the stable Git-common inode evidence that the legacy pre-1.0 implementation persisted.
    Linked worktrees are never guessed across paths because the legacy pre-1.0 implementation intentionally
    failed to distinguish them.
    """
    if target.exists():
        return None
    repos = data_home() / "repos"
    if not repos.exists():
        return None

    current = repository_identity(root)
    current_material = current.get("material") if isinstance(current.get("material"), dict) else {}
    current_common = current_material.get("git_common_inode")
    current_worktree = current_material.get("worktree_key")
    current_root = str(root.resolve())

    scored: dict[Path, int] = {}
    legacy = _legacy_repo_identity(root)
    direct = repos / str(legacy.get("key"))
    if direct.is_dir() and not direct.is_symlink() and direct != target:
        direct_state = _unchecked_state_json(direct)
        direct_ri = direct_state.get("repo_identity") if isinstance(direct_state.get("repo_identity"), dict) else {}
        direct_roots = {
            str(x) for x in (
                direct_state.get("repo_root"),
                direct_ri.get("current_root"),
            ) if x
        }
        exact_direct_root = current_root in direct_roots
        # the legacy pre-1.0 implementation gave main and linked worktrees the same key.  A direct key match is
        # therefore not enough to migrate a linked worktree unless its remembered
        # path matches exactly.  For the main worktree, a still-live distinct
        # remembered path is also treated as ambiguous rather than stolen.
        live_other_root = any(
            x != current_root and Path(x).exists()
            for x in direct_roots
        )
        if exact_direct_root:
            scored[direct] = 1000
        elif current_worktree == "main" and not live_other_root:
            scored[direct] = 800

    scanned = 0
    try:
        entries = list(repos.iterdir())
    except OSError:
        entries = []
    for candidate in entries[:4096]:
        if not candidate.is_dir() or candidate.is_symlink() or candidate == target:
            continue
        scanned += 1
        state = _unchecked_state_json(candidate)
        if not state:
            continue
        ri = state.get("repo_identity") if isinstance(state.get("repo_identity"), dict) else {}
        material = ri.get("material") if isinstance(ri.get("material"), dict) else {}
        legacy_common = material.get("git_common_inode")
        if not current_common or legacy_common != current_common:
            continue
        current_birth = current_material.get("git_common_birth")
        legacy_birth = material.get("git_common_birth")
        if current_birth and legacy_birth and current_birth != legacy_birth:
            continue
        current_worktree_birth = current_material.get("worktree_birth")
        legacy_worktree_birth = material.get("worktree_birth")
        if current_worktree_birth and legacy_worktree_birth and current_worktree_birth != legacy_worktree_birth:
            continue
        remembered_roots = {
            str(x) for x in (
                state.get("repo_root"),
                ri.get("current_root"),
            ) if x
        }
        exact_root = current_root in remembered_roots
        # the legacy pre-1.0 implementation worktrees collided by common-dir identity.  Only an exact remembered
        # path may disambiguate a linked worktree; otherwise migration must stop.
        if current_worktree != "main" and not exact_root:
            continue
        score = 900 if exact_root else 700
        scored[candidate] = max(scored.get(candidate, 0), score)

    if not scored:
        return None
    best_score = max(scored.values())
    best = sorted(p for p, score in scored.items() if score == best_score)
    if len(best) != 1:
        raise SystemExit(
            "the current implementation found multiple equally plausible the legacy pre-1.0 implementation autonomy-state directories for this repository. "
            "Automatic migration is intentionally refusing to guess; preserve the directories and reconcile them explicitly."
        )
    source = best[0]
    if _legacy_supervisor_is_active(source):
        raise SystemExit(
            f"the current implementation found active legacy supervisor state at {source}. Stop the older supervisor/service before upgrading this repository."
        )

    ensure_private_dir(target.parent)
    os.replace(source, target)
    json_dump(target / "legacy-migration.json", {
        "schema_version": 1,
        "migrated_at": utcnow(),
        "from_state_dir": str(source),
        "to_state_dir": str(target),
        "legacy_identity": legacy,
        "rc9_identity": current,
        "scanned_candidates": scanned,
    })
    return source


class SupervisorLease:
    """Exclusive local writer lease. POSIX flock releases automatically on crash/exit.

    the current implementation uses a durable guard outside the mutable per-repository state directory as
    well as the legacy in-state lock.  The outer guard lets destructive state
    maintenance (notably reset) remain fenced without deleting the lock that is
    providing the fence, while the legacy lock preserves contention with older
    the legacy pre-1.0 implementation-style supervisors that are still running.
    """
    def __init__(self, state_dir: Path, root: Path):
        self.state_dir = state_dir
        self.root = root.resolve()
        self.path = state_dir / "supervisor.lock"
        self.meta_path = state_dir / "supervisor-lease.json"
        self.guard_dir = data_home() / "leases"
        self.guard_path = self.guard_dir / f"{repo_id(self.root)}.lock"
        self.fh = None
        self.guard_fh = None
        self.token = hashlib.sha256(f"{os.getpid()}:{time.time_ns()}:{self.root}".encode()).hexdigest()[:24]

    def __enter__(self):
        ensure_private_dir(self.guard_dir)
        self.guard_fh = self.guard_path.open("a+")
        try:
            fcntl.flock(self.guard_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.guard_fh.close()
            self.guard_fh = None
            meta = load_json(self.meta_path, {})
            raise SystemExit(f"Another claude-auto supervisor already owns this repository. Existing lease: {json.dumps(meta, separators=(',', ':'))}")

        try:
            _migrate_legacy_state_if_needed(self.root, self.state_dir)
            ensure_private_dir(self.state_dir)
            self.fh = self.path.open("a+")
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                meta = load_json(self.meta_path, {})
                raise SystemExit(f"Another claude-auto supervisor already owns this repository. Existing lease: {json.dumps(meta, separators=(',', ':'))}")
            json_dump(self.meta_path, {
                "token": self.token, "pid": os.getpid(), "hostname": socket.gethostname(),
                "repo_root": str(self.root), "acquired_at": utcnow(), "active": True,
                "repo_identity": repository_identity(self.root),
                "guard_path": str(self.guard_path),
            })
            return self
        except BaseException:
            # Lock contention is reported with SystemExit, so cleanup must cover
            # BaseException rather than leaking the outer guard on that path.
            if self.fh is not None:
                try:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self.fh.close()
                self.fh = None
            if self.guard_fh is not None:
                try:
                    fcntl.flock(self.guard_fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self.guard_fh.close()
                self.guard_fh = None
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            # reset_state may intentionally remove the whole state directory while
            # the outer guard is held.  Do not recreate it merely to record release.
            if self.meta_path.parent.exists():
                current = load_json(self.meta_path, {})
                if current.get("token") == self.token:
                    current.update({"released_at": utcnow(), "active": False})
                    json_dump(self.meta_path, current)
        finally:
            if self.fh is not None:
                try:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self.fh.close()
                    self.fh = None
            if self.guard_fh is not None:
                try:
                    fcntl.flock(self.guard_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self.guard_fh.close()
                    self.guard_fh = None
        return False
