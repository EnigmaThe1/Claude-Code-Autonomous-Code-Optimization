from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from control_plane import _run_control_model, run_readonly_plan_agent
from git_trust import trusted_git_env
from promotion_policy import record_promotion_attestation
from protocols import parse_json_protocol
from provider_config import provider_from_args
from repo_identity import repo_id, repo_state_dir
from runtime_paths import ensure_private_dir, package_root, utcnow
from settings_policy import make_readonly_settings
from state_store import json_dump, load_json, sha256_text
from workspace_recovery import promote_fast_forward


PLANNING_REPAIR_CONTRACT = "repository-planning-repair"


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


def begin_planning_repair(root: Path, *, reason: str = "") -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_planning_repair_policy(root)
    if not policy:
        raise ValueError("repository-owned planning repair is not configured")
    active = load_active_repair(root)
    if active:
        return active

    product_branch = str(policy["product_branch"])
    if _current_branch(root) != product_branch:
        raise ValueError(
            f"planning repair must begin from configured product branch {product_branch!r}"
        )
    base = _rev(root, "HEAD")
    token = hashlib.sha256(
        f"{repo_id(root)}:{policy['canonical_plan']}".encode()
    ).hexdigest()[:12]
    repair_branch = f"claude-auto/planning-repair/{token}"
    worktree = _repair_dir(root) / "worktree"

    exists = _git(root, "show-ref", "--verify", f"refs/heads/{repair_branch}")
    if exists.returncode == 0:
        raise ValueError(
            f"stale planning repair branch exists without active state: {repair_branch}"
        )
    if worktree.exists():
        raise ValueError(f"stale planning repair worktree exists without active state: {worktree}")

    cp = _git(root, "worktree", "add", "-b", repair_branch, str(worktree), base)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "git worktree add failed").strip()
        raise ValueError(detail[:1600])

    active = {
        "schema_version": 1,
        "status": "ACTIVE",
        "started_at": utcnow(),
        "reason": str(reason)[:1800],
        "base_sha": base,
        "product_branch": product_branch,
        "repair_branch": repair_branch,
        "worktree": str(worktree),
        "canonical_plan": policy["canonical_plan"],
        "candidate_sha": None,
        "verified_sha": None,
        "refresh": None,
    }
    json_dump(_active_path(root), active)
    return active


def _restore_architect_worktree(worktree: Path, head: str) -> None:
    _git(worktree, "reset", "--hard", "-q", head)
    _git(worktree, "clean", "-fd", "-q", "--")


def _architect_settings(root: Path, worktree: Path, plan: Path) -> Path:
    path = _repair_dir(root) / "settings-architect.json"
    settings = {
        "env": {
            "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(worktree),
            "CLAUDE_AUTO_PLAN_REPAIR_PATH": str(plan),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "Edit|Write|NotebookEdit|Bash",
                "hooks": [{
                    "type": "command",
                    "command": f"python3 -B {package_root() / 'hooks' / 'planning_repair_guard.py'}",
                    "timeout": 5,
                }],
            }],
        },
    }
    json_dump(path, settings)
    return path


def run_planning_repair_architect(root: Path, args: Any) -> dict[str, Any]:
    root = root.expanduser().resolve()
    active = load_active_repair(root) or begin_planning_repair(
        root, reason=getattr(args, "reason", "") or ""
    )
    worktree = Path(active["worktree"]).resolve()
    plan_rel = str(active["canonical_plan"])
    plan = (worktree / plan_rel).resolve()
    before_head = _rev(worktree, "HEAD")
    dirty = _worktree_dirty_paths(worktree)
    if dirty:
        raise ValueError(
            "planning repair architect requires a clean dedicated worktree; found: "
            + ", ".join(sorted(dirty))
        )

    state = load_json(repo_state_dir(root) / "state.json", {})
    objective = str(state.get("objective") or "Preserve the repository's existing implementation objective and acceptance criteria.")
    reason = str(getattr(args, "reason", "") or active.get("reason") or "repair a concrete planning defect")
    settings = _architect_settings(root, worktree, plan)
    env, provider_detail = provider_from_args(args)
    env.update({
        "CLAUDE_AUTO_PLAN_REPAIR_ROOT": str(worktree),
        "CLAUDE_AUTO_PLAN_REPAIR_PATH": str(plan),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

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

    if changed != {plan_rel}:
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
    commit = _git(worktree, "commit", "-m", "Repair canonical implementation plan")
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
        "candidate_sha": candidate,
        "classification": classification,
        "summary": active["architect_summary"],
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
    readonly = sd / "settings-readonly.json"
    if not readonly.exists():
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
    worktree = Path(str(active.get("worktree") or ""))
    branch = str(active.get("repair_branch") or "")
    if worktree:
        cp = _git(root, "worktree", "remove", "--force", str(worktree))
        if cp.returncode != 0 and worktree.exists():
            raise ValueError("unable to remove dedicated planning repair worktree")
    if branch:
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
        root = find_repo_root(getattr(args, "repo", None))
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
