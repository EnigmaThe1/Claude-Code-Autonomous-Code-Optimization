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
import shlex
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from control_plane import _run_control_model, run_readonly_plan_agent
from operator_authority import require_top_level_operator
from git_trust import trusted_git_env
from promotion_policy import (
    REPOSITORY_PLANNING_REPAIR_CONTRACT,
    record_promotion_attestation,
)
from repair_envelope import (
    RepairEnvelopeError,
    derive_repair_envelope,
    direct_repair_path_reason,
    generated_repair_path_reason,
    load_repair_envelope,
    persist_repair_envelope,
)
from planning_helpers import (
    PlanningHelperError,
    HelperRunner,
    run_planning_reconcilers,
)
from execution import run_repository_command
from protocols import parse_json_protocol
from provider_config import provider_from_args
from repo_identity import repo_id, repo_state_dir
from runtime_paths import ensure_private_dir, package_root, utcnow
from settings_policy import make_readonly_settings
from state_store import json_dump, load_json, sha256_text
from workspace_recovery import promote_fast_forward


PLANNING_REPAIR_CONTRACT = REPOSITORY_PLANNING_REPAIR_CONTRACT


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _repair_dir(root: Path) -> Path:
    return ensure_private_dir(repo_state_dir(root.expanduser().resolve()) / "planning-repair")


def _policy_path(root: Path) -> Path:
    return _repair_dir(root) / "policy.json"


def _active_path(root: Path) -> Path:
    return _repair_dir(root) / "active.json"


def _history_path(root: Path) -> Path:
    return _repair_dir(root) / "history.jsonl"


def _append_history(root: Path, record: dict[str, Any]) -> None:
    path = _history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _normalise_plan_path(raw: str) -> str:
    path = Path(str(raw).strip())
    if not str(raw).strip() or path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError("canonical plan must be a repository-relative file path")
    return path.as_posix()


def _current_branch(root: Path) -> str:
    cp = _git(root, "branch", "--show-current")
    if cp.returncode != 0 or not cp.stdout.strip():
        raise ValueError("repository-owned planning repair requires a named product branch")
    return cp.stdout.strip()


def _rev(root: Path, value: str) -> str:
    cp = _git(root, "rev-parse", "--verify", f"{value}^{{commit}}")
    if cp.returncode != 0 or not cp.stdout.strip():
        raise ValueError(f"unable to resolve commit: {value}")
    return cp.stdout.strip().lower()


def _changed_paths(root: Path, base: str, target: str) -> set[str]:
    cp = _git(root, "diff", "--name-only", "-z", base, target, "--")
    if cp.returncode != 0:
        raise ValueError("unable to inspect planning repair candidate scope")
    return {x for x in cp.stdout.split("\0") if x}


def _worktree_dirty_paths(worktree: Path) -> set[str]:
    cp = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    if cp.returncode != 0:
        raise ValueError("unable to inspect planning repair worktree")
    out: set[str] = set()
    for row in cp.stdout.split("\0"):
        if not row:
            continue
        # porcelain v1: XY<space>PATH
        path = row[3:] if len(row) >= 4 else row
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            out.add(path)
    return out


def _require_scope(root: Path, base: str, target: str, plan_path: str) -> None:
    changed = _changed_paths(root, base, target)
    if changed != {plan_path}:
        rendered = ", ".join(sorted(changed)) or "<none>"
        raise ValueError(
            "planning repair candidate must change exactly the canonical plan path; "
            f"found: {rendered}"
        )


def configure_planning_repair(
    root: Path,
    *,
    canonical_plan: str,
    product_branch: str | None = None,
    remote: str | None = None,
    remote_branch: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if load_active_repair(root):
        raise ValueError("cannot reconfigure repository-owned planning while a repair is active")
    rel = _normalise_plan_path(canonical_plan)
    plan = root / rel
    try:
        lst = plan.lstat()
    except OSError as exc:
        raise ValueError(f"canonical plan does not exist: {rel}") from exc
    if plan.is_symlink() or not plan.is_file():
        raise ValueError("canonical plan must be a tracked regular non-symlink file")
    tracked = _git(root, "ls-files", "--error-unmatch", "--", rel)
    if tracked.returncode != 0:
        raise ValueError("canonical plan must already be tracked by Git")

    branch = product_branch or _current_branch(root)
    _rev(root, branch)
    if remote:
        check = _git(root, "remote", "get-url", remote)
        if check.returncode != 0:
            raise ValueError(f"configured planning remote does not exist: {remote}")

    policy = {
        "schema_version": 1,
        "canonical_plan": rel,
        "product_branch": branch,
        "remote": remote,
        "remote_branch": remote_branch or branch,
        "attestation_contract": PLANNING_REPAIR_CONTRACT,
        "configured_at": utcnow(),
    }
    json_dump(_policy_path(root), policy)
    return policy


def load_planning_repair_policy(root: Path) -> dict[str, Any]:
    obj = load_json(_policy_path(root), {})
    return obj if isinstance(obj, dict) else {}


def load_active_repair(root: Path) -> dict[str, Any]:
    obj = load_json(_active_path(root), {})
    return obj if isinstance(obj, dict) else {}


def planning_repair_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    return {
        "repository": str(root),
        "policy": load_planning_repair_policy(root),
        "active": load_active_repair(root),
    }


def _recover_p5_repair_worktree(
    root: Path,
    active: dict[str, Any],
) -> dict[str, Any]:
    raw_worktree = str(active.get("worktree") or "")
    repair_branch = str(active.get("repair_branch") or "")
    if not raw_worktree or not repair_branch.startswith("claude-auto/planning-repair/"):
        raise ValueError("active P5 planning repair state is incomplete or untrusted")
    worktree = Path(raw_worktree).expanduser().resolve()
    base = str(active.get("base_sha") or "")
    branch_exists = (
        _git(root, "show-ref", "--verify", f"refs/heads/{repair_branch}").returncode
        == 0
    )

    if worktree.exists() and not branch_exists:
        raise ValueError("planning repair worktree exists but its branch is missing")

    if not worktree.exists():
        if branch_exists:
            cp = _git(root, "worktree", "add", str(worktree), repair_branch)
        elif active.get("status") == "PREPARING":
            cp = _git(
                root,
                "worktree",
                "add",
                "-b",
                repair_branch,
                str(worktree),
                base,
            )
        else:
            raise ValueError(
                "active planning repair lost both package branch and worktree; "
                "automatic destructive reconstruction is refused"
            )
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "git worktree recovery failed").strip()
            raise ValueError(detail[:1600])

    if _rev(worktree, "HEAD") != base:
        raise ValueError(
            "planning repair worktree HEAD no longer matches the recorded base"
        )
    if _current_branch(worktree) != repair_branch:
        raise ValueError(
            "planning repair worktree is not on its exact recorded package branch"
        )

    if active.get("status") == "PREPARING":
        active["status"] = "ACTIVE"
        active["activated_at"] = utcnow()
    else:
        active["worktree_recovered_at"] = utcnow()
    json_dump(_active_path(root), active)
    return active


def begin_planning_repair(
    root: Path,
    *,
    reason: str = "",
    authority_sets: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root)
    if active:
        if active.get("schema_version") == 2:
            envelope = load_repair_envelope(root)
            if (
                not isinstance(envelope, dict)
                or envelope.get("repair_envelope_sha256")
                != active.get("repair_envelope_sha256")
            ):
                raise ValueError(
                    "active P5 planning repair is not bound to a valid RepairEnvelope"
                )
            return _recover_p5_repair_worktree(root, active)

        # P6 owns in-flight migration. Preserve the proven v1 recovery path.
        raw_worktree = str(active.get("worktree") or "")
        repair_branch = str(active.get("repair_branch") or "")
        if not raw_worktree or not repair_branch.startswith("claude-auto/planning-repair/"):
            raise ValueError("active planning repair state is incomplete or untrusted")
        worktree = Path(raw_worktree).expanduser().resolve()
        branch_exists = _git(
            root, "show-ref", "--verify", f"refs/heads/{repair_branch}"
        ).returncode == 0
        if worktree.exists():
            if not branch_exists:
                raise ValueError("planning repair worktree exists but its branch is missing")
            return active
        if not branch_exists:
            raise ValueError(
                "planning repair state exists but both worktree and repair branch are missing"
            )
        cp = _git(root, "worktree", "add", str(worktree), repair_branch)
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "git worktree recovery failed").strip()
            raise ValueError(detail[:1600])
        active["worktree_recovered_at"] = utcnow()
        json_dump(_active_path(root), active)
        return active

    policy = load_planning_repair_policy(root)
    if policy:
        configured_branch = str(policy["product_branch"])
        if _current_branch(root) != configured_branch:
            raise ValueError(
                f"planning repair must begin from configured product branch {configured_branch!r}"
            )

    try:
        envelope = persist_repair_envelope(
            root,
            derive_repair_envelope(
                root,
                reason=reason,
                authority_sets=authority_sets,
            ),
        )
    except RepairEnvelopeError as exc:
        if not policy:
            raise ValueError(str(exc)) from exc
        raise ValueError(f"unable to derive RepairEnvelope: {exc}") from exc

    if policy and envelope["product_branch"] != policy["product_branch"]:
        raise ValueError(
            "legacy planning policy product branch and RepairEnvelope branch disagree"
        )

    token = hashlib.sha256(
        (
            f"{repo_id(root)}:{envelope['repair_envelope_sha256']}:"
            f"{envelope['base_sha']}"
        ).encode()
    ).hexdigest()[:12]
    repair_branch = f"claude-auto/planning-repair/{token}"
    worktree = _repair_dir(root) / "worktree"

    exists = _git(root, "show-ref", "--verify", f"refs/heads/{repair_branch}")
    if exists.returncode == 0:
        raise ValueError(
            f"stale planning repair branch exists without active state: {repair_branch}"
        )
    if worktree.exists():
        raise ValueError(
            f"stale planning repair worktree exists without active state: {worktree}"
        )

    canonical_plan = (
        str(policy.get("canonical_plan"))
        if policy and policy.get("canonical_plan")
        else None
    )
    active = {
        "schema_version": 2,
        "status": "PREPARING",
        "started_at": utcnow(),
        "reason": str(reason)[:1800],
        "base_sha": envelope["base_sha"],
        "product_branch": envelope["product_branch"],
        "repair_branch": repair_branch,
        "worktree": str(worktree),
        "canonical_plan": canonical_plan,
        "repair_envelope_sha256": envelope["repair_envelope_sha256"],
        "selected_authority_sets": envelope["selected_authority_sets"],
        "candidate_sha": None,
        "verified_sha": None,
        "refresh": None,
    }
    # Durable intent precedes the first Git topology mutation.
    json_dump(_active_path(root), active)
    return _recover_p5_repair_worktree(root, active)


def _restore_architect_worktree(worktree: Path, head: str) -> None:
    _git(worktree, "reset", "--hard", "-q", head)
    _git(worktree, "clean", "-fd", "-q", "--")


def _architect_settings(
    root: Path,
    worktree: Path,
    *,
    plan: Path | None = None,
    envelope_path: Path | None = None,
) -> Path:
    path = _repair_dir(root) / "settings-architect.json"
    env = {
        "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if plan is not None:
        env["CLAUDE_AUTO_PLAN_REPAIR_PATH"] = str(plan)
    if envelope_path is not None:
        env["CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE"] = str(envelope_path)
    settings = {
        "env": env,
        "hooks": {
            "PreToolUse": [{
                "matcher": "Edit|Write|NotebookEdit|Bash",
                "hooks": [{
                    "type": "command",
                    "command": "python3 -B " + shlex.quote(
                        str(package_root() / "hooks" / "planning_repair_guard.py")
                    ),
                    "timeout": 30,
                }],
            }],
        },
    }
    json_dump(path, settings)
    return path


def _architect_delete_paths(
    worktree: Path,
    envelope: dict[str, Any],
    raw: Any,
) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("PLANNING_REPAIR_ARCHITECT.delete_paths must be a bounded list")
    out: list[str] = []
    seen: set[str] = set()
    base_repairable = set(envelope.get("repairable_paths", []))
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("architect delete_paths entries must be non-empty strings")
        rel = Path(item.strip()).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        reason = direct_repair_path_reason(worktree, envelope, rel)
        if reason:
            raise ValueError(f"architect deletion denied for {rel}: {reason}")
        if rel not in base_repairable:
            raise ValueError(
                f"architect may delete only an existing base repairable member: {rel}"
            )
        target = worktree / rel
        try:
            if target.is_symlink() or not target.is_file():
                raise ValueError(
                    f"architect deletion target must be a regular planning file: {rel}"
                )
        except OSError as exc:
            raise ValueError(f"unable to inspect deletion target {rel}: {exc}") from exc
        out.append(rel)
    return sorted(out)


def run_planning_repair_architect(root: Path, args: Any) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root) or begin_planning_repair(
        root,
        reason=getattr(args, "reason", "") or "",
        authority_sets=getattr(args, "authority_set", None),
    )
    worktree = Path(active["worktree"]).resolve()
    is_p5 = active.get("schema_version") == 2
    envelope = load_repair_envelope(root) if is_p5 else None
    if is_p5 and (
        not isinstance(envelope, dict)
        or envelope.get("repair_envelope_sha256")
        != active.get("repair_envelope_sha256")
    ):
        raise ValueError("active P5 repair is not bound to its RepairEnvelope")

    canonical_raw = active.get("canonical_plan")
    plan_rel = str(canonical_raw) if isinstance(canonical_raw, str) and canonical_raw else None
    plan = (worktree / plan_rel).resolve() if plan_rel else None
    envelope_path = (
        _repair_dir(root) / "repair-envelope.json"
        if is_p5
        else None
    )
    before_head = _rev(worktree, "HEAD")
    dirty = _worktree_dirty_paths(worktree)
    if dirty:
        raise ValueError(
            "planning repair architect requires a clean dedicated worktree; found: "
            + ", ".join(sorted(dirty))
        )

    state = load_json(repo_state_dir(root) / "state.json", {})
    objective = str(
        state.get("objective")
        or "Preserve the repository's existing implementation objective and acceptance criteria."
    )
    reason = str(
        getattr(args, "reason", "")
        or active.get("reason")
        or "repair a concrete planning defect"
    )
    settings = _architect_settings(
        root,
        worktree,
        plan=plan,
        envelope_path=envelope_path,
    )
    env, provider_detail = provider_from_args(args)
    env.update({
        "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if plan is not None:
        env["CLAUDE_AUTO_PLAN_REPAIR_PATH"] = str(plan)
    if envelope_path is not None:
        env["CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE"] = str(envelope_path)

    if is_p5:
        selected = ", ".join(envelope["selected_authority_sets"])
        prompt = textwrap.dedent(f"""
        You are the dedicated Planning Repair Architect for repository-owned planning authority.

        PRODUCT OBJECTIVE (authoritative):
        {objective}

        SELECTED AUTHORITY SETS:
        {selected}

        REPAIR REASON:
        {reason}

        Work only in the dedicated planning worktree. Direct file mutations are
        permitted only when the package RepairEnvelope guard allows them.
        You may directly edit repairable planning members. You may NOT directly
        edit immutable members, generated members, governance/control state or
        another AuthoritySet. Generated members are package-owned reconciler outputs.

        If an existing repairable planning member must be deleted, do not use
        Bash. Request the exact repository-relative path in delete_paths.

        Preserve the product objective and existing required functionality.
        Repair contradictions, dependency/order mistakes, missing implementation
        work required by existing requirements, verification gaps and assumptions
        contradicted by repository evidence. Do NOT add unrelated product features
        or broaden product scope.

        If the repair requires a genuine semantic product decision not already
        resolved by objective/repository evidence, make no repair and classify
        SEMANTIC_DECISION.

        Before finishing, analyse backward impact on accepted/completed work and
        forward impact on remaining work.

        End with exactly one JSON protocol record:
        PLANNING_REPAIR_ARCHITECT:
        {{"verdict":"READY|BLOCKED","classification":"PLAN_PRESERVING|SEMANTIC_DECISION","summary":"...","delete_paths":[]}}
        """).strip()
    else:
        prompt = textwrap.dedent(f"""
        You are the dedicated Planning Repair Architect for a repository-owned canonical implementation plan.

        PRODUCT OBJECTIVE (authoritative):
        {objective}

        CANONICAL PLAN FILE:
        {plan_rel}

        REPAIR REASON:
        {reason}

        Work only in the dedicated planning worktree. You may edit ONLY the canonical plan file.
        Preserve the product objective and existing required functionality. Repair contradictions,
        dependency/order mistakes, missing implementation work needed by existing requirements,
        verification gaps, and plan assumptions contradicted by repository reality.
        Do NOT add unrelated product features or broaden product scope.

        If the repair would require a genuine semantic product decision not already resolved by the
        objective/repository evidence, do not edit the plan and classify SEMANTIC_DECISION.

        Before finishing, inspect the resulting plan for backward impact on completed work and forward
        impact on remaining work.

        End with exactly one JSON protocol record:
        PLANNING_REPAIR_ARCHITECT: {{"verdict":"READY|BLOCKED","classification":"PLAN_PRESERVING|SEMANTIC_DECISION","summary":"..."}}
        """).strip()

    cmd = [
        "claude",
        "--settings", str(settings),
        "--setting-sources", "",
        "--tools", "Read,Glob,Grep,Edit,Write",
        "--disallowed-tools", "Bash,NotebookEdit,AskUserQuestion,mcp__*",
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--effort", "high",
    ]
    model = getattr(args, "model", None)
    if model:
        cmd += ["--model", model]
    budget = getattr(args, "max_budget_usd", None)
    if budget is not None:
        cmd += ["--max-budget-usd", str(budget)]
    cmd += [
        "--print", "--output-format", "json",
        "--max-turns", str(getattr(args, "max_turns", 40) or 40),
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        prompt,
    ]

    cp, result_text, session_id, _raw, outcome, outcome_reason, usage, attempts, wall_seconds = _run_control_model(
        cmd=cmd,
        root=worktree,
        sd=repo_state_dir(root),
        env=env,
        timeout=(getattr(args, "timeout", 0) or None),
        max_transient_retries=4,
    )
    protocol = parse_json_protocol(result_text, "PLANNING_REPAIR_ARCHITECT")
    changed = _worktree_dirty_paths(worktree)

    if cp.returncode != 0 or not protocol:
        _restore_architect_worktree(worktree, before_head)
        raise ValueError(
            f"planning repair architect did not complete safely: {outcome}: {outcome_reason}"
        )

    verdict = str(protocol.get("verdict", "BLOCKED")).upper()
    classification = str(protocol.get("classification", "SEMANTIC_DECISION")).upper()
    if verdict != "READY" or classification != "PLAN_PRESERVING":
        _restore_architect_worktree(worktree, before_head)
        active["last_architect"] = {
            "verdict": verdict,
            "classification": classification,
            "summary": str(protocol.get("summary", ""))[:1800],
            "session_id": session_id,
            "at": utcnow(),
        }
        json_dump(_active_path(root), active)
        return {
            "status": "blocked",
            "classification": classification,
            "summary": str(protocol.get("summary", ""))[:1800],
        }

    if is_p5:
        assert envelope is not None
        for rel in sorted(changed):
            denial = direct_repair_path_reason(worktree, envelope, rel)
            if denial:
                _restore_architect_worktree(worktree, before_head)
                raise ValueError(
                    f"planning repair architect changed denied path {rel}: {denial}"
                )
        try:
            delete_paths = _architect_delete_paths(
                worktree,
                envelope,
                protocol.get("delete_paths", []),
            )
        except ValueError:
            _restore_architect_worktree(worktree, before_head)
            raise
        for rel in delete_paths:
            (worktree / rel).unlink()
        changed = _worktree_dirty_paths(worktree)
        for rel in sorted(changed):
            denial = direct_repair_path_reason(worktree, envelope, rel)
            if denial:
                _restore_architect_worktree(worktree, before_head)
                raise ValueError(
                    f"planning repair delta is outside RepairEnvelope at {rel}: {denial}"
                )

        active.update({
            "architect_classification": classification,
            "architect_summary": str(protocol.get("summary", ""))[:1800],
            "architect_delete_paths": delete_paths,
            "architect_session_id": session_id,
            "architect_provider": provider_detail,
            "architect_usage": usage,
            "architect_attempts": attempts,
            "architect_wall_seconds": wall_seconds,
            "verified_sha": None,
        })
        if envelope.get("reconciler_contracts"):
            active["status"] = "RECONCILING"
            active["candidate_sha"] = None
            active["reconciliation_required_at"] = utcnow()
            json_dump(_active_path(root), active)
            return {
                "status": "reconcile-required",
                "classification": classification,
                "repair_envelope_sha256": envelope["repair_envelope_sha256"],
                "changed_paths": sorted(changed),
                "summary": active["architect_summary"],
            }
        if not changed:
            _restore_architect_worktree(worktree, before_head)
            raise ValueError("planning repair architect produced no RepairEnvelope change")
        stage = _git(worktree, "add", "-A", "--", *sorted(changed))
        if stage.returncode != 0:
            _restore_architect_worktree(worktree, before_head)
            raise ValueError("unable to stage RepairEnvelope-admitted planning changes")
        commit = _git(
            worktree,
            "-c", "user.name=Claude Code Autonomous Optimization",
            "-c", "user.email=claude-auto@localhost.invalid",
            "-c", "commit.gpgSign=false",
            "-c", "core.hooksPath=/dev/null",
            "commit", "-m", "Repair repository planning authority",
        )
        if commit.returncode != 0:
            _restore_architect_worktree(worktree, before_head)
            detail = (commit.stderr or commit.stdout or "git commit failed").strip()
            raise ValueError(detail[:1600])
        candidate = _rev(worktree, "HEAD")
        candidate_paths = _changed_paths(
            worktree,
            str(active["base_sha"]),
            candidate,
        )
        for rel in sorted(candidate_paths):
            denial = direct_repair_path_reason(worktree, envelope, rel)
            if denial:
                _restore_architect_worktree(worktree, before_head)
                raise ValueError(
                    f"candidate commit escaped RepairEnvelope at {rel}: {denial}"
                )
        active.update({
            "status": "CANDIDATE",
            "candidate_sha": candidate,
            "candidate_created_at": utcnow(),
        })
    else:
        if plan_rel is None or changed != {plan_rel}:
            _restore_architect_worktree(worktree, before_head)
            raise ValueError(
                "planning repair architect changed files outside the canonical plan: "
                + (", ".join(sorted(changed)) or "<none>")
            )
        add = _git(worktree, "add", "--", plan_rel)
        if add.returncode != 0:
            _restore_architect_worktree(worktree, before_head)
            raise ValueError("unable to stage repaired canonical plan")
        staged = _git(worktree, "diff", "--cached", "--quiet", "--", plan_rel)
        if staged.returncode == 0:
            _restore_architect_worktree(worktree, before_head)
            raise ValueError("planning repair architect produced no canonical plan change")
        commit = _git(
            worktree,
            "-c", "user.name=Claude Code Autonomous Optimization",
            "-c", "user.email=claude-auto@localhost.invalid",
            "-c", "commit.gpgSign=false",
            "commit", "-m", "Repair canonical implementation plan",
        )
        if commit.returncode != 0:
            _restore_architect_worktree(worktree, before_head)
            detail = (commit.stderr or commit.stdout or "git commit failed").strip()
            raise ValueError(detail[:1600])
        candidate = _rev(worktree, "HEAD")
        _require_scope(worktree, str(active["base_sha"]), candidate, plan_rel)
        active.update({
            "candidate_sha": candidate,
            "verified_sha": None,
            "architect_classification": classification,
            "architect_summary": str(protocol.get("summary", ""))[:1800],
            "architect_session_id": session_id,
            "architect_provider": provider_detail,
            "architect_usage": usage,
            "architect_attempts": attempts,
            "architect_wall_seconds": wall_seconds,
            "candidate_created_at": utcnow(),
        })

    json_dump(_active_path(root), active)
    return {
        "status": "candidate",
        "candidate_sha": active["candidate_sha"],
        "classification": classification,
        "summary": active["architect_summary"],
    }


def _tracked_planning_deletions(worktree: Path) -> set[str]:
    cp = _git(
        worktree,
        "diff",
        "--name-only",
        "--diff-filter=D",
        "-z",
        "HEAD",
        "--",
    )
    if cp.returncode != 0:
        raise ValueError("unable to inspect planning repair deletions")
    return {item for item in cp.stdout.split("\0") if item}


def _p5_delta_path_reason(
    coordinator_root: Path,
    worktree: Path,
    envelope: dict[str, Any],
    rel: str,
) -> tuple[str | None, str | None]:
    direct = direct_repair_path_reason(worktree, envelope, rel)
    if direct is None:
        return "repairable", None
    generated = generated_repair_path_reason(
        coordinator_root,
        envelope,
        rel,
    )
    if generated is None:
        return "generated", None
    return None, (
        f"repairable denial: {direct}; generated denial: {generated}"
    )


def run_planning_repair_reconcile(
    root: Path,
    *,
    runner: HelperRunner = run_repository_command,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root)
    if not active or active.get("schema_version") != 2:
        raise ValueError("P5 reconciliation requires an active schema-2 planning repair")
    if active.get("status") != "RECONCILING":
        raise ValueError(
            "planning reconciliation requires RECONCILING state; "
            f"found {active.get('status')!r}"
        )

    envelope = load_repair_envelope(root)
    if (
        not isinstance(envelope, dict)
        or envelope.get("repair_envelope_sha256")
        != active.get("repair_envelope_sha256")
    ):
        raise ValueError("active planning repair is not bound to its RepairEnvelope")
    contracts = list(envelope.get("reconciler_contracts", []))
    if not contracts:
        raise ValueError("active RECONCILING repair has no reconciler contracts")

    worktree = Path(active["worktree"]).expanduser().resolve()
    base = str(active["base_sha"])
    if _rev(worktree, "HEAD") != base:
        raise ValueError("planning reconciler requires worktree HEAD at the exact repair base")

    dirty_before = _worktree_dirty_paths(worktree)
    if not dirty_before:
        raise ValueError("planning reconciliation requires an Architect repair delta")

    approved_deletes = set(active.get("architect_delete_paths") or [])
    tracked_deletes = _tracked_planning_deletions(worktree)
    for rel in sorted(dirty_before):
        kind, denial = _p5_delta_path_reason(root, worktree, envelope, rel)
        if denial:
            raise ValueError(
                f"planning reconciliation found out-of-envelope dirty path {rel}: {denial}"
            )
        if (
            kind == "repairable"
            and rel in tracked_deletes
            and rel not in approved_deletes
        ):
            raise ValueError(
                f"repairable deletion was not package-approved by the Architect protocol: {rel}"
            )

    try:
        bundle = run_planning_reconcilers(
            root,
            worktree,
            envelope,
            runner=runner,
        )
    except PlanningHelperError as exc:
        active["last_reconciliation_error"] = str(exc)[:1800]
        active["reconciliation_failed_at"] = utcnow()
        json_dump(_active_path(root), active)
        raise ValueError(str(exc)) from exc

    receipt_path = _repair_dir(root) / "reconciler-receipts.json"
    json_dump(receipt_path, bundle)

    dirty_after = _worktree_dirty_paths(worktree)
    if not dirty_after:
        raise ValueError(
            "planning reconciliation produced no RepairEnvelope-admitted candidate delta"
        )

    output_paths = {
        row["path"]
        for receipt in bundle.get("receipts", [])
        for row in receipt.get("output_state", [])
    }
    tracked_deletes = _tracked_planning_deletions(worktree)
    for rel in sorted(dirty_after):
        kind, denial = _p5_delta_path_reason(root, worktree, envelope, rel)
        if denial:
            raise ValueError(
                f"planning reconciliation produced denied candidate path {rel}: {denial}"
            )
        if (
            kind == "repairable"
            and rel in tracked_deletes
            and rel not in approved_deletes
        ):
            raise ValueError(
                f"repairable deletion was not package-approved by the Architect protocol: {rel}"
            )
        if kind == "generated" and rel not in output_paths:
            raise ValueError(
                f"generated candidate path was not produced by a reconciler receipt: {rel}"
            )

    stage = _git(worktree, "add", "-A", "--", *sorted(dirty_after))
    if stage.returncode != 0:
        detail = (stage.stderr or stage.stdout or "git add failed").strip()
        raise ValueError(f"unable to stage reconciled planning candidate: {detail[:1600]}")
    commit = _git(
        worktree,
        "-c", "user.name=Claude Code Autonomous Optimization",
        "-c", "user.email=claude-auto@localhost.invalid",
        "-c", "commit.gpgSign=false",
        "-c", "core.hooksPath=/dev/null",
        "commit", "-m", "Repair repository planning authority",
    )
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "git commit failed").strip()
        raise ValueError(detail[:1600])

    candidate = _rev(worktree, "HEAD")
    candidate_paths = _changed_paths(worktree, base, candidate)
    for rel in sorted(candidate_paths):
        _kind, denial = _p5_delta_path_reason(root, worktree, envelope, rel)
        if denial:
            raise ValueError(
                f"reconciled candidate commit escaped RepairEnvelope at {rel}: {denial}"
            )

    active.update({
        "status": "CANDIDATE",
        "candidate_sha": candidate,
        "candidate_created_at": utcnow(),
        "verified_sha": None,
        "reconciler_receipt_bundle_sha256": bundle[
            "reconciler_receipt_bundle_sha256"
        ],
        "reconciler_receipt_path": str(receipt_path),
        "reconciled_at": utcnow(),
    })
    active.pop("last_reconciliation_error", None)
    json_dump(_active_path(root), active)
    return {
        "status": "candidate",
        "candidate_sha": candidate,
        "repair_envelope_sha256": envelope["repair_envelope_sha256"],
        "reconciler_receipt_bundle_sha256": bundle[
            "reconciler_receipt_bundle_sha256"
        ],
        "changed_paths": sorted(candidate_paths),
        "receipts": bundle.get("receipts", []),
    }


def verify_planning_repair(root: Path, args: Any) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root)
    if not active:
        raise ValueError("no active planning repair exists")
    worktree = Path(active["worktree"]).resolve()
    candidate = str(getattr(args, "sha", None) or active.get("candidate_sha") or "")
    candidate = _rev(worktree, candidate)
    if candidate != _rev(worktree, "HEAD"):
        raise ValueError("planning verifier requires the exact current repair-branch HEAD")
    plan_rel = str(active["canonical_plan"])
    base = str(active["base_sha"])
    _require_scope(worktree, base, candidate, plan_rel)

    sd = repo_state_dir(root)
    readonly = _repair_dir(root) / "settings-verifier-readonly.json"
    json_dump(readonly, make_readonly_settings(sd, worktree))
    state = load_json(sd / "state.json", {})
    objective = str(state.get("objective") or "Preserve the repository's existing implementation objective and acceptance criteria.")
    diff = _git(worktree, "diff", "--no-ext-diff", "--binary", base, candidate, "--", plan_rel)
    if diff.returncode != 0:
        raise ValueError("unable to capture exact planning repair diff")
    evidence = {
        "base_sha": base,
        "candidate_sha": candidate,
        "canonical_plan": plan_rel,
        "objective": objective,
        "repair_reason": active.get("reason"),
        "architect_summary": active.get("architect_summary"),
        "diff": diff.stdout,
    }
    evidence_path = _repair_dir(root) / f"verify-{candidate[:16]}.json"
    json_dump(evidence_path, evidence)

    env, provider_detail = provider_from_args(args)
    prompt = textwrap.dedent(f"""
    You are the independent Planning Verifier. You did not author this repair.
    Operate HARD READ-ONLY and verify the exact candidate SHA below.

    EXACT CANDIDATE SHA: {candidate}
    BASE SHA: {base}
    CANONICAL PLAN: {plan_rel}
    PRODUCT OBJECTIVE: {objective}

    PRIVATE EVIDENCE FILE:
    {evidence_path}

    Inspect repository reality and the exact plan diff. Verify that the repair:
    - preserves the product objective and required functionality;
    - does not introduce unrelated features or scope expansion;
    - resolves the stated planning defect coherently;
    - remains dependency/order consistent;
    - accounts for backward impact on completed work and forward impact on remaining work;
    - changes only the canonical plan.

    If any material issue exists, reject it. Your verdict is bound to EXACT CANDIDATE SHA.

    Return exactly one JSON protocol record:
    PLANNING_REPAIR_VERIFY: {{"verdict":"VERIFIED|REJECTED","summary":"...","findings":["..."]}}
    """).strip()

    result_text, meta = run_readonly_plan_agent(
        root=worktree,
        sd=sd,
        prompt=prompt,
        env=env,
        provider_detail=provider_detail,
        model=getattr(args, "model", None),
        timeout=(getattr(args, "timeout", 0) or None),
        max_turns=int(getattr(args, "max_turns", 35) or 35),
        verify_repo=True,
        max_budget_usd=getattr(args, "max_budget_usd", None),
        settings_path=readonly,
    )
    protocol = parse_json_protocol(result_text, "PLANNING_REPAIR_VERIFY")
    if not protocol:
        raise ValueError("planning verifier returned no valid protocol")
    verdict = str(protocol.get("verdict", "REJECTED")).upper()
    findings = protocol.get("findings") if isinstance(protocol.get("findings"), list) else []
    summary = str(protocol.get("summary", ""))[:1800]
    if verdict != "VERIFIED":
        active["verified_sha"] = None
        active["last_verifier"] = {
            "verdict": verdict,
            "summary": summary,
            "findings": findings,
            "meta": meta,
            "at": utcnow(),
        }
        json_dump(_active_path(root), active)
        return {"status": "rejected", "candidate_sha": candidate, "summary": summary, "findings": findings}

    evidence_digest = sha256_text(json.dumps({
        "evidence": evidence,
        "verifier_result": protocol,
        "git_after": meta.get("git_after"),
    }, sort_keys=True, separators=(",", ":"), default=str))
    attestation = record_promotion_attestation(
        root,
        target_sha=candidate,
        contract=PLANNING_REPAIR_CONTRACT,
        verifier=f"planning-verifier:{getattr(args, 'model', None) or 'native-default'}",
        evidence_sha256=evidence_digest,
        summary=summary,
        metadata={
            "provider": provider_detail,
            "findings": findings,
            "repository_unchanged": meta.get("repository_unchanged"),
        },
    )
    active.update({
        "verified_sha": candidate,
        "verifier_summary": summary,
        "verifier_findings": findings,
        "verifier_attestation": attestation,
        "verified_at": utcnow(),
    })
    json_dump(_active_path(root), active)
    return {
        "status": "verified",
        "candidate_sha": candidate,
        "attestation": attestation,
        "summary": summary,
    }


def refresh_planning_repair_base(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_planning_repair_policy(root)
    active = load_active_repair(root)
    if not policy or not active:
        raise ValueError("no configured active planning repair exists")
    worktree = Path(active["worktree"]).resolve()
    branch = str(active["repair_branch"])
    old_base = str(active["base_sha"])
    new_base = _rev(root, str(policy["product_branch"]))
    candidate = _rev(worktree, branch)
    plan_rel = str(active["canonical_plan"])

    if new_base == old_base:
        return {"status": "unchanged", "base_sha": old_base, "candidate_sha": candidate}

    old_to_new = _git(root, "merge-base", "--is-ancestor", old_base, new_base)
    if old_to_new.returncode != 0:
        raise ValueError("new product base is not a descendant of the planning repair base")

    if candidate == old_base and not active.get("candidate_sha"):
        if _worktree_dirty_paths(worktree):
            raise ValueError("planning repair worktree must be clean before refreshing its empty candidate base")
        reset = _git(worktree, "reset", "--hard", "-q", new_base)
        if reset.returncode != 0:
            raise ValueError("unable to advance empty planning repair worktree to the new product base")
        active.update({
            "base_sha": new_base,
            "candidate_sha": None,
            "verified_sha": None,
            "refresh": None,
            "refreshed_at": utcnow(),
        })
        json_dump(_active_path(root), active)
        return {"status": "advanced-empty", "base_sha": new_base, "candidate_sha": None}

    new_in_candidate = _git(worktree, "merge-base", "--is-ancestor", new_base, candidate)
    if new_in_candidate.returncode == 0:
        _require_scope(worktree, new_base, candidate, plan_rel)
        active.update({
            "base_sha": new_base,
            "candidate_sha": candidate,
            "verified_sha": None,
            "refresh": None,
            "refreshed_at": utcnow(),
        })
        json_dump(_active_path(root), active)
        return {"status": "already-absorbed", "base_sha": new_base, "candidate_sha": candidate}

    refresh = active.get("refresh") if isinstance(active.get("refresh"), dict) else None
    if refresh and refresh.get("in_progress"):
        _git(worktree, "rebase", "--abort")
        original = str(refresh.get("candidate_before") or "")
        if original:
            _git(worktree, "checkout", "-q", branch)
            _git(worktree, "reset", "--hard", "-q", original)
        candidate = _rev(worktree, branch)

    if _worktree_dirty_paths(worktree):
        raise ValueError("planning repair worktree must be clean before refreshing its base")

    active["refresh"] = {
        "in_progress": True,
        "old_base": old_base,
        "new_base": new_base,
        "candidate_before": candidate,
        "started_at": utcnow(),
    }
    json_dump(_active_path(root), active)

    cp = _git(worktree, "rebase", "--onto", new_base, old_base, branch)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "git rebase failed").strip()
        active = load_active_repair(root)
        active["refresh_failure"] = detail[:1600]
        json_dump(_active_path(root), active)
        raise ValueError(
            "planning repair base refresh was interrupted/failed; rerun refresh-base to abort/reconcile/retry: "
            + detail[:1200]
        )

    candidate_after = _rev(worktree, branch)
    _require_scope(worktree, new_base, candidate_after, plan_rel)
    active = load_active_repair(root)
    active.update({
        "base_sha": new_base,
        "candidate_sha": candidate_after,
        "verified_sha": None,
        "verifier_attestation": None,
        "refresh": None,
        "refresh_failure": None,
        "refreshed_at": utcnow(),
    })
    json_dump(_active_path(root), active)
    return {"status": "rebased", "base_sha": new_base, "candidate_sha": candidate_after}


def _cleanup_repair_worktree(root: Path, active: dict[str, Any]) -> None:
    raw_worktree = str(active.get("worktree") or "")
    branch = str(active.get("repair_branch") or "")
    if not raw_worktree or not branch.startswith("claude-auto/planning-repair/"):
        raise ValueError("refusing cleanup of unrecognised planning repair state")
    worktree = Path(raw_worktree).expanduser().resolve()
    allowed_parent = _repair_dir(root).resolve()
    try:
        worktree.relative_to(allowed_parent)
    except ValueError as exc:
        raise ValueError("refusing cleanup outside package-owned planning repair state") from exc
    if worktree == allowed_parent:
        raise ValueError("refusing cleanup of planning repair state root")

    cp = _git(root, "worktree", "remove", "--force", str(worktree))
    if cp.returncode != 0 and worktree.exists():
        raise ValueError("unable to remove dedicated planning repair worktree")
    cp = _git(root, "branch", "-D", branch)
    if cp.returncode != 0:
        # If the ref disappeared with an interrupted prior cleanup, continue
        # only when Git confirms it is already absent.
        exists = _git(root, "show-ref", "--verify", f"refs/heads/{branch}")
        if exists.returncode == 0:
            raise ValueError("unable to remove completed planning repair branch")


def promote_planning_repair(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_planning_repair_policy(root)
    active = load_active_repair(root)
    if not policy or not active:
        raise ValueError("no configured active planning repair exists")
    candidate = str(active.get("candidate_sha") or "")
    verified = str(active.get("verified_sha") or "")
    if not candidate or verified != candidate:
        raise ValueError("planning repair promotion requires VERIFIED attestation for the exact active candidate SHA")

    remote = policy.get("remote") if isinstance(policy.get("remote"), str) and policy.get("remote") else None
    result = promote_fast_forward(
        root,
        candidate,
        attestation_contract=PLANNING_REPAIR_CONTRACT,
        remote=remote,
        remote_branch=str(policy.get("remote_branch") or policy["product_branch"]) if remote else None,
        expected_remote=str(active["base_sha"]) if remote else None,
    )
    completed = dict(active)
    completed.update({
        "status": "PROMOTED",
        "promotion": result,
        "completed_at": utcnow(),
    })
    _append_history(root, completed)
    _cleanup_repair_worktree(root, active)
    try:
        _active_path(root).unlink()
    except FileNotFoundError:
        pass
    return result


def abort_planning_repair(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root)
    if not active:
        return {"status": "no-active-repair"}
    archived = dict(active)
    archived.update({"status": "ABORTED", "completed_at": utcnow()})
    _append_history(root, archived)
    _cleanup_repair_worktree(root, active)
    try:
        _active_path(root).unlink()
    except FileNotFoundError:
        pass
    return {"status": "aborted"}


def planning_repair_action(args: Any, *, find_repo_root) -> int:
    try:
        action = getattr(args, "planning_repair_command", None)
        root = find_repo_root(getattr(args, "repo", None))
        if action in ["configure","begin","architect","verify","refresh-base","promote","abort"]:
            require_top_level_operator(root, "Repository planning authority change")
        action = getattr(args, "planning_repair_command", None)
        if action == "status":
            result = planning_repair_status(root)
        elif action == "configure":
            result = configure_planning_repair(
                root,
                canonical_plan=args.plan,
                product_branch=getattr(args, "product_branch", None),
                remote=getattr(args, "remote", None),
                remote_branch=getattr(args, "remote_branch", None),
            )
        elif action == "begin":
            result = begin_planning_repair(root, reason=getattr(args, "reason", "") or "")
        elif action == "architect":
            result = run_planning_repair_architect(root, args)
        elif action == "verify":
            result = verify_planning_repair(root, args)
        elif action == "refresh-base":
            result = refresh_planning_repair_base(root)
        elif action == "promote":
            result = promote_planning_repair(root)
        elif action == "abort":
            result = abort_planning_repair(root)
        else:
            raise ValueError("unknown planning-repair action")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0
