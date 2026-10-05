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

import hashlib
import json
import os
import shutil
import stat
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from authority_set import AuthoritySetError, build_authority_snapshot
from git_trust import trusted_git_env
from governance_contract import (
    GovernanceContractError,
    canonical_json_bytes,
    load_governance_contract,
)
from repo_identity import repo_state_dir
from runtime_paths import ensure_private_dir, package_root, utcnow
from state_store import json_dump, load_json
from task_sources import TaskSourceError, load_resolved_task_source_set
from task_spec import selector_matches_path


class ExecutionEnvelopeError(ValueError):
    pass


_SEMANTIC_KEYS = (
    "schema_version",
    "task_id",
    "task_source_set_sha256",
    "task_spec_sha256",
    "authority_snapshot_sha256",
    "product_base_sha",
    "direct_edit_paths",
    "promotion_paths",
    "runtime_scratch_paths",
    "protected_paths",
    "baseline_wip_sha256",
    "accepted_tasks_sha256",
)

_ROOT_CONTROL_FILES = {
    ".mcp.json",
    ".gitattributes",
    ".gitmodules",
    "CLAUDE.md",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
}

_CONTROL_PREFIXES = (
    ".git/",
    ".claude-auto/",
    ".claude/",
    ".github/workflows/",
    ".circleci/",
    ".buildkite/",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git(
    root: Path,
    *args: str,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _git_bytes(root: Path, *args: str) -> bytes:
    cp = _git(root, *args, text=False)
    if cp.returncode != 0:
        detail = bytes(cp.stderr or cp.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        raise ExecutionEnvelopeError(
            f"Git command failed while deriving task authority: {' '.join(args)}: {detail[:1200]}"
        )
    return bytes(cp.stdout)


def _decode_git_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionEnvelopeError(
            "task authority requires UTF-8 repository paths; a non-UTF-8 Git path was encountered"
        ) from exc
    return unicodedata.normalize("NFC", value)


def _nul_paths(root: Path, *args: str) -> list[str]:
    payload = _git_bytes(root, *args)
    return sorted(
        {_decode_git_path(item) for item in payload.split(b"\0") if item},
        key=lambda item: item.encode("utf-8"),
    )


def git_head(root: Path) -> str:
    cp = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if cp.returncode != 0 or not str(cp.stdout).strip():
        raise ExecutionEnvelopeError("task authority requires a valid Git HEAD commit")
    return str(cp.stdout).strip().lower()


def git_branch(root: Path) -> str | None:
    cp = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if cp.returncode == 0 and str(cp.stdout).strip():
        return str(cp.stdout).strip()
    if cp.returncode == 1:
        return None
    raise ExecutionEnvelopeError("unable to determine the current Git branch")


def _path_identity(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"path": rel, "kind": "missing"}
    except OSError as exc:
        raise ExecutionEnvelopeError(
            f"unable to inspect repository path {rel!r}: {exc}"
        ) from exc

    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise ExecutionEnvelopeError(
                f"unable to read symlink repository path {rel!r}: {exc}"
            ) from exc
        return {
            "path": rel,
            "kind": "symlink",
            "mode": mode,
            "target": target,
        }

    if stat.S_ISREG(st.st_mode):
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError as exc:
            raise ExecutionEnvelopeError(
                f"unable to hash repository path {rel!r}: {exc}"
            ) from exc
        return {
            "path": rel,
            "kind": "file",
            "mode": mode,
            "size": st.st_size,
            "sha256": h.hexdigest(),
        }

    if stat.S_ISDIR(st.st_mode):
        return {"path": rel, "kind": "directory", "mode": mode}

    return {
        "path": rel,
        "kind": "other",
        "mode": mode,
        "device": getattr(st, "st_rdev", 0),
    }


def _records(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    return [_path_identity(root, rel) for rel in sorted(set(paths))]


def capture_workspace_baseline(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    head = git_head(root)
    staged = _nul_paths(root, "diff", "--cached", "--name-only", "-z", "--")
    unstaged = _nul_paths(root, "diff", "--name-only", "-z", "--no-ext-diff", "--")
    untracked = _nul_paths(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    baseline = {
        "schema_version": 1,
        "head": head,
        "staged_paths": staged,
        "unstaged": _records(root, unstaged),
        "untracked": _records(root, untracked),
    }
    baseline["baseline_wip_sha256"] = _digest({
        key: baseline[key]
        for key in ("schema_version", "head", "staged_paths", "unstaged", "untracked")
    })
    return baseline


def _record_map(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ExecutionEnvelopeError("workspace baseline records are malformed")
    out: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ExecutionEnvelopeError("workspace baseline contains a malformed path record")
        path = item["path"]
        if path in out:
            raise ExecutionEnvelopeError("workspace baseline contains a duplicate path")
        out[path] = item
    return out


def _verify_baseline_digest(baseline: dict[str, Any]) -> str:
    recorded = baseline.get("baseline_wip_sha256")
    if not isinstance(recorded, str) or len(recorded) != 64:
        raise ExecutionEnvelopeError("workspace baseline digest is missing or malformed")
    actual = _digest({
        key: baseline.get(key)
        for key in ("schema_version", "head", "staged_paths", "unstaged", "untracked")
    })
    if actual != recorded:
        raise ExecutionEnvelopeError("workspace baseline integrity check failed")
    return recorded


def path_matches_any(rel: str, selectors: Iterable[str]) -> bool:
    rel = unicodedata.normalize("NFC", rel)
    return any(selector_matches_path(selector, rel) for selector in selectors)


def hard_control_path(rel: str) -> bool:
    rel = unicodedata.normalize("NFC", rel).strip("/")
    if not rel:
        return True
    if rel in {".git", ".claude-auto", ".claude", ".github/workflows", ".circleci", ".buildkite"}:
        return True
    if rel in _ROOT_CONTROL_FILES:
        return True
    if rel.endswith("/CLAUDE.md"):
        return True
    return any(rel.startswith(prefix) for prefix in _CONTROL_PREFIXES)


def path_relates_to_protected(rel: str, protected_paths: Iterable[str]) -> bool:
    rel = unicodedata.normalize("NFC", rel).strip("/")
    if hard_control_path(rel):
        return True
    for raw in protected_paths:
        protected = unicodedata.normalize("NFC", str(raw)).strip("/")
        if (
            rel == protected
            or rel.startswith(protected + "/")
            or protected.startswith(rel + "/")
        ):
            return True
    return False


def selector_covers_subtree(selector: str, rel: str) -> bool:
    rel = unicodedata.normalize("NFC", rel).strip("/")
    if selector_matches_path(selector, rel):
        return True
    probe = f"{rel}/__claude_auto_subtree_probe__" if rel else "__claude_auto_subtree_probe__"
    if selector_matches_path(selector, probe):
        return True
    literal_prefix = selector.split("*", 1)[0].split("?", 1)[0].rstrip("/")
    return bool(literal_prefix and (literal_prefix == rel or literal_prefix.startswith(rel + "/")))


def _gitlinks(root: Path) -> list[str]:
    payload = _git_bytes(root, "ls-files", "-s", "-z")
    out: list[str] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
            mode = meta.split(b" ", 1)[0]
        except ValueError as exc:
            raise ExecutionEnvelopeError("unexpected Git index record while checking gitlinks") from exc
        if mode == b"160000":
            out.append(_decode_git_path(raw_path))
    return sorted(set(out))


def _task_touches_gitlink(
    root: Path,
    selectors: Iterable[str],
) -> str | None:
    selectors = list(selectors)
    for gitlink in _gitlinks(root):
        if any(selector_covers_subtree(selector, gitlink) for selector in selectors):
            return gitlink
    return None


def _sparse_checkout_enabled(root: Path) -> bool:
    cp = _git(root, "config", "--bool", "core.sparseCheckout")
    return cp.returncode == 0 and str(cp.stdout).strip().lower() == "true"


def _envelope_path(root: Path) -> Path:
    return repo_state_dir(root) / "tasks" / "execution-envelope.json"


def _violation_path(root: Path) -> Path:
    return repo_state_dir(root) / "tasks" / "violation.json"


def _history_dir(root: Path) -> Path:
    return ensure_private_dir(repo_state_dir(root) / "tasks" / "history")


def _task_record(task_source_set: dict[str, Any], task_id: str) -> dict[str, Any]:
    rows = task_source_set.get("tasks")
    if not isinstance(rows, list):
        raise ExecutionEnvelopeError("TaskSourceSet task records are malformed")
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("task"), dict)
        and row["task"].get("id") == task_id
    ]
    if len(matches) != 1:
        raise ExecutionEnvelopeError(
            f"TaskSpec {task_id!r} does not resolve uniquely in the current TaskSourceSet"
        )
    return matches[0]


def prepare_execution_envelope(
    root: Path,
    *,
    task_record: dict[str, Any],
    task_source_set: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    task = task_record.get("task")
    if not isinstance(task, dict):
        raise ExecutionEnvelopeError("selected TaskSpec record is malformed")
    task_id = task.get("id")
    task_spec_sha256 = task_record.get("task_spec_sha256")
    if not isinstance(task_id, str) or not isinstance(task_spec_sha256, str):
        raise ExecutionEnvelopeError("selected TaskSpec identity is malformed")

    if git_branch(root) is None:
        raise ExecutionEnvelopeError(
            "task activation requires a named branch; detached HEAD is read-only in P3"
        )
    if _sparse_checkout_enabled(root):
        raise ExecutionEnvelopeError(
            "task activation is blocked for sparse checkouts in P3; required task paths may be unmaterialised"
        )

    expected_head = task_source_set.get("product_head")
    current_head = git_head(root)
    if current_head != expected_head:
        raise ExecutionEnvelopeError(
            f"task activation base changed: TaskSourceSet={expected_head}, HEAD={current_head}"
        )

    try:
        snapshot = build_authority_snapshot(root)
    except AuthoritySetError as exc:
        raise ExecutionEnvelopeError(str(exc)) from exc
    if not isinstance(snapshot, dict):
        raise ExecutionEnvelopeError("task activation requires current repository governance")
    if snapshot.get("snapshot_sha256") != task_source_set.get("authority_snapshot_sha256"):
        raise ExecutionEnvelopeError("TaskSourceSet and current AuthoritySet snapshot do not match")

    direct = sorted(set(
        list(task.get("owned_paths") or [])
        + list(task.get("evidence_paths") or [])
    ))
    promotion = list(direct)
    scratch = sorted(set(task.get("runtime_scratch_paths") or []))
    protected = sorted(set(snapshot.get("protected_paths") or []))

    gitlink = _task_touches_gitlink(root, [*direct, *scratch])
    if gitlink is not None:
        raise ExecutionEnvelopeError(
            f"task selectors cross unsupported gitlink/submodule boundary: {gitlink}"
        )

    baseline = capture_workspace_baseline(root)
    if baseline["staged_paths"]:
        raise ExecutionEnvelopeError(
            "task activation requires an empty index; pre-existing staged WIP is present: "
            + ", ".join(baseline["staged_paths"][:40])
        )

    baseline_paths = [
        item["path"]
        for key in ("unstaged", "untracked")
        for item in baseline[key]
    ]
    for rel in baseline_paths:
        if path_relates_to_protected(rel, protected):
            raise ExecutionEnvelopeError(
                f"pre-existing WIP touches protected semantic authority: {rel}"
            )
        if path_matches_any(rel, [*direct, *scratch]):
            raise ExecutionEnvelopeError(
                f"pre-existing WIP overlaps selected task authority: {rel}"
            )

    accepted_tasks = state.get("accepted_tasks")
    if not isinstance(accepted_tasks, dict):
        accepted_tasks = {}

    semantic = {
        "schema_version": 1,
        "task_id": task_id,
        "task_source_set_sha256": task_source_set.get("task_source_set_sha256"),
        "task_spec_sha256": task_spec_sha256,
        "authority_snapshot_sha256": snapshot.get("snapshot_sha256"),
        "product_base_sha": current_head,
        "direct_edit_paths": direct,
        "promotion_paths": promotion,
        "runtime_scratch_paths": scratch,
        "protected_paths": protected,
        "baseline_wip_sha256": baseline["baseline_wip_sha256"],
        "accepted_tasks_sha256": _digest(accepted_tasks),
    }
    digest = _digest(semantic)
    return {
        **semantic,
        "execution_envelope_sha256": digest,
        "baseline_wip": baseline,
        "created_at": utcnow(),
    }


def persist_execution_envelope(root: Path, record: dict[str, Any]) -> None:
    path = _envelope_path(root.expanduser().resolve())
    ensure_private_dir(path.parent)
    json_dump(path, record)


def _read_json_exact(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ExecutionEnvelopeError(f"{label} does not exist")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionEnvelopeError(f"{label} is unreadable or malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionEnvelopeError(f"{label} must be a JSON object")
    return value


def _semantic_from_record(record: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _SEMANTIC_KEYS if key not in record]
    if missing:
        raise ExecutionEnvelopeError(
            "ExecutionEnvelope is missing semantic field(s): " + ", ".join(missing)
        )
    return {key: record[key] for key in _SEMANTIC_KEYS}


def load_active_execution_envelope(
    root: Path,
    *,
    require_current: bool = True,
    require_head: bool = True,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state = load_json(repo_state_dir(root) / "state.json", {})
    if not isinstance(state, dict):
        raise ExecutionEnvelopeError("durable repository state is malformed")

    task_id = state.get("active_task_id")
    task_sha = state.get("active_task_spec_sha256")
    envelope_sha = state.get("active_execution_envelope_sha256")
    if not all(isinstance(value, str) and value for value in (task_id, task_sha, envelope_sha)):
        raise ExecutionEnvelopeError("no active ExecutionEnvelope is bound to durable state")

    record = _read_json_exact(_envelope_path(root), label="active ExecutionEnvelope")
    semantic = _semantic_from_record(record)
    actual = _digest(semantic)
    if actual != record.get("execution_envelope_sha256") or actual != envelope_sha:
        raise ExecutionEnvelopeError("ExecutionEnvelope semantic integrity check failed")
    if semantic["task_id"] != task_id or semantic["task_spec_sha256"] != task_sha:
        raise ExecutionEnvelopeError("ExecutionEnvelope identity does not match durable state")

    baseline = record.get("baseline_wip")
    if not isinstance(baseline, dict):
        raise ExecutionEnvelopeError("ExecutionEnvelope workspace baseline is missing")
    if _verify_baseline_digest(baseline) != semantic["baseline_wip_sha256"]:
        raise ExecutionEnvelopeError("ExecutionEnvelope baseline binding is invalid")

    try:
        task_set = load_resolved_task_source_set(
            root,
            require_current=require_current,
            require_state_binding=True,
        )
    except TaskSourceError as exc:
        raise ExecutionEnvelopeError(str(exc)) from exc
    if task_set.get("task_source_set_sha256") != semantic["task_source_set_sha256"]:
        raise ExecutionEnvelopeError("ExecutionEnvelope TaskSourceSet binding is stale")

    task_record = _task_record(task_set, task_id)
    if task_record.get("task_spec_sha256") != semantic["task_spec_sha256"]:
        raise ExecutionEnvelopeError("ExecutionEnvelope TaskSpec digest is stale")

    task = task_record["task"]
    expected_direct = sorted(set(
        list(task.get("owned_paths") or [])
        + list(task.get("evidence_paths") or [])
    ))
    if semantic["direct_edit_paths"] != expected_direct or semantic["promotion_paths"] != expected_direct:
        raise ExecutionEnvelopeError("ExecutionEnvelope path authority does not match the TaskSpec")
    if semantic["runtime_scratch_paths"] != sorted(set(task.get("runtime_scratch_paths") or [])):
        raise ExecutionEnvelopeError("ExecutionEnvelope scratch authority does not match the TaskSpec")

    if require_current:
        accepted = state.get("accepted_tasks")
        if not isinstance(accepted, dict):
            accepted = {}
        if _digest(accepted) != semantic["accepted_tasks_sha256"]:
            raise ExecutionEnvelopeError("accepted-task state changed since activation")
        try:
            snapshot = build_authority_snapshot(root)
        except AuthoritySetError as exc:
            raise ExecutionEnvelopeError(str(exc)) from exc
        if not isinstance(snapshot, dict) or snapshot.get("snapshot_sha256") != semantic["authority_snapshot_sha256"]:
            raise ExecutionEnvelopeError("ExecutionEnvelope governance binding is stale")

    if require_head and git_head(root) != semantic["product_base_sha"]:
        raise ExecutionEnvelopeError("repository HEAD moved away from the active ExecutionEnvelope base")
    return record


def current_staged_entries(root: Path, *, ref: str = "HEAD") -> list[dict[str, Any]]:
    payload = _git_bytes(
        root,
        "diff",
        "--cached",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        ref,
        "--",
    )
    fields = [item for item in payload.split(b"\0") if item]
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        status_raw = fields[index]
        index += 1
        status = status_raw.decode("ascii", errors="replace")
        if index >= len(fields):
            raise ExecutionEnvelopeError("unexpected staged diff record")
        first = _decode_git_path(fields[index])
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(fields):
                raise ExecutionEnvelopeError("unexpected staged rename/copy record")
            second = _decode_git_path(fields[index])
            index += 1
            out.append({"status": status, "paths": [first, second]})
        else:
            out.append({"status": status, "paths": [first]})
    return out


def _mode_at(root: Path, ref: str, rel: str) -> str | None:
    cp = _git(root, "ls-tree", "-z", ref, "--", rel, text=False)
    if cp.returncode != 0:
        raise ExecutionEnvelopeError(f"unable to inspect Git mode for {rel!r} at {ref!r}")
    payload = bytes(cp.stdout)
    for row in payload.split(b"\0"):
        if not row:
            continue
        try:
            meta, raw_path = row.split(b"\t", 1)
            mode = meta.split(b" ", 1)[0].decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExecutionEnvelopeError("unexpected Git tree mode record") from exc
        if _decode_git_path(raw_path) == rel:
            return mode
    return None


def _index_mode(root: Path, rel: str) -> str | None:
    cp = _git(root, "ls-files", "-s", "-z", "--", rel, text=False)
    if cp.returncode != 0:
        raise ExecutionEnvelopeError(f"unable to inspect staged mode for {rel!r}")
    payload = bytes(cp.stdout)
    for row in payload.split(b"\0"):
        if not row:
            continue
        try:
            meta, raw_path = row.split(b"\t", 1)
            mode = meta.split(b" ", 1)[0].decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExecutionEnvelopeError("unexpected Git index mode record") from exc
        if _decode_git_path(raw_path) == rel:
            return mode
    return None


def _admission_reason(record: dict[str, Any], rel: str) -> str | None:
    protected = record["protected_paths"]
    promotion = record["promotion_paths"]
    scratch = record["runtime_scratch_paths"]
    if path_relates_to_protected(rel, protected):
        return "protected semantic/control path"
    if path_matches_any(rel, scratch):
        return "runtime scratch is never promotable"
    if not path_matches_any(rel, promotion):
        return "path is outside the active task promotion envelope"
    return None


def validate_staged_diff(
    root: Path,
    *,
    envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    record = envelope or load_active_execution_envelope(root)
    if git_head(root) != record["product_base_sha"]:
        raise ExecutionEnvelopeError("staged-diff validation requires the exact envelope base HEAD")

    entries = current_staged_entries(root)
    violations: list[dict[str, str]] = []
    for entry in entries:
        for rel in entry["paths"]:
            reason = _admission_reason(record, rel)
            if reason:
                violations.append({"path": rel, "reason": reason})
            base_mode = _mode_at(root, record["product_base_sha"], rel)
            index_mode = _index_mode(root, rel)
            if base_mode == "160000" or index_mode == "160000":
                violations.append({
                    "path": rel,
                    "reason": "gitlink/submodule changes are not supported by P3 task admission",
                })

    if violations:
        unique = {
            (item["path"], item["reason"]): item
            for item in violations
        }
        rows = [unique[key] for key in sorted(unique)]
        raise ExecutionEnvelopeError(
            "staged diff violates the active task envelope: "
            + "; ".join(f"{item['path']}: {item['reason']}" for item in rows[:40])
        )
    return {
        "status": "VALID",
        "task_id": record["task_id"],
        "execution_envelope_sha256": record["execution_envelope_sha256"],
        "staged_entries": entries,
    }


def _baseline_exact_now(root: Path, baseline: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for key in ("unstaged", "untracked"):
        rows = _record_map(baseline.get(key))
        for rel, expected in rows.items():
            actual = _path_identity(root, rel)
            if actual != expected:
                problems.append({
                    "path": rel,
                    "reason": "pre-existing WIP changed during the active task",
                })
    return problems


def _current_delta_paths(root: Path, baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    current_unstaged = set(
        _nul_paths(root, "diff", "--name-only", "-z", "--no-ext-diff", "--")
    )
    current_untracked = set(
        _nul_paths(root, "ls-files", "--others", "--exclude-standard", "-z")
    )
    baseline_unstaged = set(_record_map(baseline.get("unstaged")))
    baseline_untracked = set(_record_map(baseline.get("untracked")))
    return (
        sorted(current_unstaged - baseline_unstaged),
        sorted(current_untracked - baseline_untracked),
    )


def evaluate_active_workspace(
    root: Path,
    *,
    envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    record = envelope or load_active_execution_envelope(root)
    violations: list[dict[str, str]] = []

    if git_head(root) != record["product_base_sha"]:
        violations.append({
            "path": "HEAD",
            "reason": "repository HEAD moved away from the active task base",
        })

    baseline = record["baseline_wip"]
    violations.extend(_baseline_exact_now(root, baseline))
    new_unstaged, new_untracked = _current_delta_paths(root, baseline)
    for rel in sorted(set(new_unstaged + new_untracked)):
        if path_relates_to_protected(rel, record["protected_paths"]):
            violations.append({"path": rel, "reason": "protected semantic/control path changed"})
            continue
        if not path_matches_any(
            rel,
            [*record["direct_edit_paths"], *record["runtime_scratch_paths"]],
        ):
            violations.append({
                "path": rel,
                "reason": "repository mutation is outside the active task envelope",
            })

    try:
        validate_staged_diff(root, envelope=record)
    except ExecutionEnvelopeError as exc:
        violations.append({"path": "INDEX", "reason": str(exc)})

    unique = {
        (item["path"], item["reason"]): item
        for item in violations
    }
    rows = [unique[key] for key in sorted(unique)]
    return {
        "status": "VALID" if not rows else "VIOLATION",
        "task_id": record["task_id"],
        "execution_envelope_sha256": record["execution_envelope_sha256"],
        "new_unstaged_paths": new_unstaged,
        "new_untracked_paths": new_untracked,
        "violations": rows,
    }


def workspace_matches_activation_baseline(
    root: Path,
    *,
    envelope: dict[str, Any] | None = None,
) -> bool:
    root = root.expanduser().resolve()
    record = envelope or load_active_execution_envelope(root)
    baseline = record["baseline_wip"]
    if git_head(root) != record["product_base_sha"]:
        return False
    current = capture_workspace_baseline(root)
    return (
        current["staged_paths"] == baseline["staged_paths"]
        and current["unstaged"] == baseline["unstaged"]
        and current["untracked"] == baseline["untracked"]
    )


def record_task_violation(
    root: Path,
    *,
    envelope: dict[str, Any],
    violations: list[dict[str, Any]],
    tool_batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    core = {
        "schema_version": 1,
        "execution_envelope_sha256": envelope["execution_envelope_sha256"],
        "task_id": envelope["task_id"],
        "product_base_sha": envelope["product_base_sha"],
        "observed_head": git_head(root),
        "violations": violations,
        "workspace_state_sha256": _digest(capture_workspace_baseline(root)),
        "tool_batch": tool_batch if isinstance(tool_batch, dict) else {},
    }
    record = {
        **core,
        "violation_sha256": _digest(core),
        "recorded_at": utcnow(),
    }
    path = _violation_path(root)
    ensure_private_dir(path.parent)
    json_dump(path, record)
    return record


def record_task_authority_failure(
    root: Path,
    *,
    reason: str,
    tool_batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state = load_json(repo_state_dir(root) / "state.json", {})
    if not isinstance(state, dict):
        state = {}
    try:
        observed_head = git_head(root)
    except ExecutionEnvelopeError:
        observed_head = "unknown"
    core = {
        "schema_version": 1,
        "execution_envelope_sha256": str(
            state.get("active_execution_envelope_sha256") or "unbound"
        ),
        "task_id": str(state.get("active_task_id") or "unbound"),
        "product_base_sha": "unknown",
        "observed_head": observed_head,
        "violations": [{"path": "AUTHORITY", "reason": str(reason)[:2000]}],
        "workspace_state_sha256": "unavailable",
        "tool_batch": tool_batch if isinstance(tool_batch, dict) else {},
    }
    record = {
        **core,
        "violation_sha256": _digest(core),
        "recorded_at": utcnow(),
    }
    path = _violation_path(root)
    ensure_private_dir(path.parent)
    json_dump(path, record)
    return record


def load_task_violation(root: Path) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    path = _violation_path(root)
    if not path.exists():
        return None
    record = _read_json_exact(path, label="task violation record")
    required = (
        "schema_version",
        "execution_envelope_sha256",
        "task_id",
        "product_base_sha",
        "observed_head",
        "violations",
        "workspace_state_sha256",
        "tool_batch",
    )
    core = {key: record.get(key) for key in required}
    if record.get("violation_sha256") != _digest(core):
        raise ExecutionEnvelopeError("task violation record integrity check failed")
    return record


def clear_task_violation(root: Path) -> None:
    try:
        _violation_path(root.expanduser().resolve()).unlink()
    except FileNotFoundError:
        pass


def invalidate_task_authority_after_head_change(
    root: Path,
    *,
    reason: str,
) -> None:
    root = root.expanduser().resolve()
    state_path = repo_state_dir(root) / "state.json"
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        raise ExecutionEnvelopeError("durable repository state is malformed")

    envelope_path = _envelope_path(root)
    if envelope_path.exists():
        try:
            record = _read_json_exact(envelope_path, label="active ExecutionEnvelope")
            digest = str(record.get("execution_envelope_sha256") or "unknown")
        except ExecutionEnvelopeError:
            digest = "corrupt"
        target = _history_dir(root) / f"execution-envelope-{digest[:64]}.json"
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(envelope_path), str(target))
        except OSError as exc:
            raise ExecutionEnvelopeError(
                f"unable to archive stale ExecutionEnvelope after HEAD change: {exc}"
            ) from exc

    violation_path = _violation_path(root)
    if violation_path.exists():
        target = _history_dir(root) / f"violation-{hashlib.sha256(reason.encode()).hexdigest()[:20]}-{int(os.getpid())}.json"
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(violation_path), str(target))
        except OSError:
            pass

    state["task_source_sha256"] = None
    state["active_task_id"] = None
    state["active_task_spec_sha256"] = None
    state["active_execution_envelope_sha256"] = None
    state["task_authority_invalidated_reason"] = str(reason)[:1000]
    state["task_authority_invalidated_at"] = utcnow()
    json_dump(state_path, state)


def external_semantic_protected_paths(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    out = [repo_state_dir(root).resolve()]
    pkg = package_root().resolve()
    try:
        pkg.relative_to(root)
    except ValueError:
        out.append(pkg)
    return out


def absolute_path_hits_external_semantic_state(root: Path, target: Path) -> bool:
    try:
        resolved = target.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True
    for protected in external_semantic_protected_paths(root):
        if resolved == protected:
            return True
        try:
            resolved.relative_to(protected)
            return True
        except ValueError:
            pass
        try:
            protected.relative_to(resolved)
            return True
        except ValueError:
            pass
    return False


def repository_relative_lexical_path(root: Path, target: Path) -> str | None:
    root = root.expanduser().resolve()
    try:
        absolute = Path(os.path.abspath(str(target)))
        rel = absolute.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    text = unicodedata.normalize("NFC", rel.as_posix())
    return text if text and text != "." else "."


def path_has_symlink_component(root: Path, rel: str) -> bool:
    root = root.expanduser().resolve()
    current = root
    parts = Path(rel).parts
    for part in parts:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
    return False


def task_owned_mode(root: Path) -> bool:
    root = root.expanduser().resolve()
    try:
        contract = load_governance_contract(root)
    except GovernanceContractError as exc:
        # The write guard is also used by RC3-compatible/non-Git project roots.
        # Absence of any repository governance contract means task-owned mode is
        # simply not configured. If a contract file is present but Git-backed
        # governance cannot be verified, fail closed instead.
        if not (root / ".claude-auto" / "governance.json").exists():
            return False
        raise ExecutionEnvelopeError(str(exc)) from exc
    if not isinstance(contract, dict):
        return False
    tasks = contract.get("tasks")
    return bool(isinstance(tasks, dict) and tasks.get("sources"))


def direct_repository_mutation_reason(
    root: Path,
    *,
    rel: str,
    allow_scratch: bool = False,
) -> str | None:
    root = root.expanduser().resolve()
    # Repository-declared TaskSources switch product mutation into task-owned
    # mode even before the sources have been resolved. Unresolved/stale task
    # authority must block, never silently fall back to RC3 repository-wide
    # mutation authority.
    if not task_owned_mode(root):
        return None

    try:
        envelope = load_active_execution_envelope(root)
    except ExecutionEnvelopeError as exc:
        return f"task-owned repository has no current active ExecutionEnvelope: {exc}"

    if path_relates_to_protected(rel, envelope["protected_paths"]):
        return "path is protected semantic/control authority"
    if rel == ".":
        return "repository-root mutation is not a task-scoped direct edit"
    if path_has_symlink_component(root, rel):
        return "task direct edits may not traverse repository symlinks"
    if path_matches_any(rel, envelope["direct_edit_paths"]):
        return None
    if allow_scratch and path_matches_any(rel, envelope["runtime_scratch_paths"]):
        return None
    return "path is outside the active task direct-edit envelope"


def promotion_target_entries(
    root: Path,
    *,
    base: str,
    target: str,
) -> list[dict[str, Any]]:
    payload = _git_bytes(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        base,
        target,
        "--",
    )
    fields = [item for item in payload.split(b"\0") if item]
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="replace")
        index += 1
        if index >= len(fields):
            raise ExecutionEnvelopeError("unexpected promotion diff record")
        first = _decode_git_path(fields[index])
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(fields):
                raise ExecutionEnvelopeError("unexpected promotion rename/copy record")
            second = _decode_git_path(fields[index])
            index += 1
            out.append({"status": status, "paths": [first, second]})
        else:
            out.append({"status": status, "paths": [first]})
    return out


def validate_promotion_target_for_active_envelope(
    root: Path,
    *,
    base: str,
    target: str,
) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    state = load_json(repo_state_dir(root) / "state.json", {})
    if not isinstance(state, dict) or not state.get("active_execution_envelope_sha256"):
        return None
    envelope = load_active_execution_envelope(root)
    violation = load_task_violation(root)
    if violation is not None:
        raise ExecutionEnvelopeError("task promotion is blocked by an unresolved task-envelope violation")
    if base.lower() != envelope["product_base_sha"]:
        raise ExecutionEnvelopeError(
            "active task promotion must start at the exact ExecutionEnvelope base"
        )

    entries = promotion_target_entries(root, base=base, target=target)
    problems: list[str] = []
    for entry in entries:
        for rel in entry["paths"]:
            reason = _admission_reason(envelope, rel)
            if reason:
                problems.append(f"{rel}: {reason}")
            base_mode = _mode_at(root, base, rel)
            target_mode = _mode_at(root, target, rel)
            if base_mode == "160000" or target_mode == "160000":
                problems.append(f"{rel}: gitlink/submodule changes are not supported by P3")
    if problems:
        raise ExecutionEnvelopeError(
            "promotion target violates the active task envelope: "
            + "; ".join(sorted(set(problems))[:40])
        )
    return {
        "status": "VALID",
        "task_id": envelope["task_id"],
        "execution_envelope_sha256": envelope["execution_envelope_sha256"],
        "entries": entries,
    }
